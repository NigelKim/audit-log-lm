# Dataloader for EHR audit log dataset, based on vocabulary generation from Padhi et al. (2021)

import os
import pickle

import numpy as np
import torch
from torch.utils.data import Dataset, IterableDataset
from typing import List
import pandas as pd

from model.vocab import EHRVocab


class EHRAuditDataset(IterableDataset):
    """
    Dataset for Epic EHR audit log data.

    Assumes that the data is associated with a single physician.
    Users IDs are transformed to the # in which they are encountered for a given shift.
    Shifts are a gap in time set by hyperparameter, and have 0 entropy to start.
    Separation of shifts and sessions are delineated by the same process.
    Time deltas are calculated w.r.t. the preceding event.
    """

    def __init__(
        self,
        root_dir: str,
        session_sep_min: int = 4,
        shift_sep_min: int = 300,
        user_col: str = "PAT_ID",
        timestamp_col: str = "ACCESS_TIME",
        timestamp_sort_cols: List[str] = ["ACCESS_TIME", "ACCESS_INSTANT"],
        event_type_cols: List[str] = ["METRIC_NAME"],
        log_name: str = None,
        vocab: EHRVocab = None,
        timestamp_spaces: List[float] = None,
        should_tokenize: bool = True,
        cache: str = None,
        max_length: int = None,
    ):
        self.seqs = []
        self.len = None
        self.provider = os.path.basename(root_dir)
        self.session_sep_min = session_sep_min
        self.shift_sep_min = shift_sep_min
        self.user_col = user_col
        self.timestamp_col = timestamp_col
        self.event_type_cols = event_type_cols
        self.log_name = log_name
        self.root_dir = root_dir
        self.vocab = vocab
        self.timestamp_spaces = timestamp_spaces
        self.should_tokenize = should_tokenize
        self.timestamp_sort_cols = timestamp_sort_cols
        self.max_length = max_length

        if self.timestamp_spaces is None and self.should_tokenize is True:
            raise ValueError("Tokenization depends on timestamp binning.")
        self.cache = cache

    def load(self):
        """
        Load the dataset from either a log file or a cache.
        """
        if self.cache is not None and os.path.exists(
            os.path.normpath(os.path.join(self.root_dir, self.cache))
        ):
            self.load_from_cache()
        else:
            self.load_from_log()

    def load_from_log(self):
        """
        Load the dataset from a log file.
        """
        print(f"Loading {self.provider} from {self.log_name}")
        path = os.path.normpath(os.path.join(self.root_dir, self.log_name))
        df = pd.read_csv(path)

        # Ensure that timestamp_col is in timestamp_sort_cols
        if self.timestamp_col not in self.timestamp_sort_cols:
            raise ValueError(
                f"timestamp_col {self.timestamp_col} must be in timestamp_sort_cols"
            )

        # Delete all columns not included
        df = df[[self.user_col] + self.timestamp_sort_cols + self.event_type_cols]

        # Convert the timestamp to time deltas.
        # If not in seconds, convert to seconds.
        if df[self.timestamp_col].dtype == np.dtype("O"):
            df[self.timestamp_col] = pd.to_datetime(df[self.timestamp_col])
            df[self.timestamp_col] = df[self.timestamp_col].astype(np.int64) // 10**9

        # Sort by timestamp
        df = df.sort_values(by=self.timestamp_sort_cols)
        # Delete the timestamp_sort_cols except for the timestamp_col
        df = df.drop(columns=set(self.timestamp_sort_cols) - {self.timestamp_col})

        # Time deltas, (ignore negative values, these will be quantized away)
        df.loc[:, self.timestamp_col] = df.loc[:, self.timestamp_col].copy().diff()

        # Set beginning of shift to 0, otherwise it's nan.
        df.loc[0, self.timestamp_col] = 0

        # Separate the data into shifts.
        sep_sec = self.shift_sep_min * 60
        seqs_shifts = []
        seq_start_idx = 0
        for i, row in df.iterrows():
            if row[self.timestamp_col] > sep_sec:
                df.loc[i, self.timestamp_col] = 0
                seq_end_idx = i
                seqs_shifts.append(df.iloc[seq_start_idx:seq_end_idx, :].copy())
                seq_start_idx = seq_end_idx

        # Append the last shift
        seqs_shifts.append(df.iloc[seq_start_idx:, :].copy())

        # Convert the user IDs in each shift to a unique integer
        # Missing user IDs are handled below.
        for seq in seqs_shifts:
            seq[self.user_col] = seq[self.user_col].astype("category").cat.codes

        # Separate the data into sessions.
        sep_sec = self.session_sep_min * 60
        seqs = []
        seq_start_idx = 0
        for shift in seqs_shifts:
            for i, row in shift.iterrows():
                if row[self.timestamp_col] > sep_sec:
                    shift.loc[i, self.timestamp_col] = 0  # Reset the time delta to 0.
                    seq_end_idx = i
                    seqs.append(shift.iloc[seq_start_idx:seq_end_idx, :].copy())
                    seq_start_idx = seq_end_idx

            # Append the last shift
            seqs.append(shift.iloc[seq_start_idx:, :].copy())

        # Also convert the events to the corresponding vocab value.
        self.seqs = seqs

        if self.timestamp_spaces is not None:
            for s in self.seqs:
                s.loc[:, self.timestamp_col] = s.loc[:, self.timestamp_col].apply(
                    lambda x: np.digitize(np.log(x + 1), self.timestamp_spaces)
                )

        if self.should_tokenize:
            tokenized_cols = [self.user_col, self.timestamp_col] + self.event_type_cols
            tokenized_seqs = []
            for s in self.seqs:  # Iterate each sequence
                tokenized_example = []
                for i, row in s.iterrows():  # Iterate each row
                    tokenized_example.extend(
                        [
                            self.vocab.field_to_token(col, str(row[col]))
                            for col in tokenized_cols
                        ]
                    )

                # Add the end of sequence token.
                tokenized_example.append(
                    self.vocab.field_to_token("special", self.vocab.eos_token)
                )

                # Convert the sequence to a tensor.
                tokenized_example = torch.tensor(tokenized_example)

                # Split up the sequence into chunks of max_length if not None
                if self.max_length is not None:
                    chunk_size = self.max_length // len(tokenized_cols)
                    tokenized_example = torch.split(tokenized_example, chunk_size)
                tokenized_seqs.append(tokenized_example)

            self.seqs = tokenized_seqs

            self.len = len(self.seqs)

        if self.cache is not None:
            # Save the metadata (length) of the dataset, as well as the tensorized sequence.
            cache_path = os.path.normpath(os.path.join(self.root_dir, self.cache))
            if not os.path.exists(cache_path):
                os.makedirs(cache_path)

            with open(
                os.path.normpath(os.path.join(cache_path, "length.pkl")), "wb"
            ) as f:
                pickle.dump(self.len, f)

            torch.save(self.seqs, os.path.normpath(os.path.join(cache_path, "seqs.pt")))

    def load_from_cache(self, length=True, seqs=False):
        """
        Load the dataset from a cached file.
        Deliberately only load parts as needed.
        """
        cache_path = os.path.normpath(os.path.join(self.root_dir, self.cache))
        if not os.path.exists(cache_path):
            raise ValueError("Cache does not exist.")

        if length:
            with open(
                os.path.normpath(os.path.join(cache_path, "length.pkl")), "rb"
            ) as f:
                try:
                    self.len = pickle.load(f)
                except EOFError:
                    self.len = 0  # Maybe a better way to handle this?

        if seqs:
            self.seqs = torch.load(
                os.path.normpath(os.path.join(cache_path, "seqs.pt"))
            )

    def __getitem__(self, item):
        if self.seqs == [] and os.path.exists(
            os.path.normpath(os.path.join(self.root_dir, self.cache))
        ):
            self.load_from_cache(seqs=True)
        return self.seqs[item]

    def __len__(self):
        if self.len is None and os.path.exists(
            os.path.normpath(os.path.join(self.root_dir, self.cache))
        ):
            self.load_from_cache(length=True)
        return self.len  # Returns None if not cached or loaded.
