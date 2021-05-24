#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import abc

class StorageProvider(abc.ABC):
    SCHEME: str

    @abc.abstractmethod
    def download_blob(self, url: str) -> bytes:
        ...

    @abc.abstractmethod
    def upload_blob(self, url: str, body: bytes) -> None:
        ...

    @abc.abstractmethod
    def download_file(self, url: str, path: str) -> None:
        ...

    @abc.abstractmethod
    def upload_file(self, path: str, url: str) -> None:
        ...
