#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import Tuple, Optional
from urllib.parse import urlparse

from torchx.runtime.storage_provider import StorageProvider

from fsspec.core import get_fs_token_paths
from fsspec.spec import AbstractFileSystem
from fsspec.registry import get_filesystem_class


class FSSpecProvider(StorageProvider):
    def __init__(self, scheme: str) -> None:
        self.SCHEME = scheme
        super().__init__()

    def _get_fs(self, url: str) -> Tuple[AbstractFileSystem, str]:
        fs, _, paths = get_fs_token_paths(url)
        assert len(paths) == 1, f"must have exactly 1 path - got {paths}"

        return fs, paths[0]

    def download_blob(self, url: str) -> bytes:
        """
        download_blob fetches the contents of the file located at the URL.
        """
        fs, rpath = self._get_fs(url)
        with fs.open(rpath, "rb") as f:
            return f.read()

    def upload_blob(self, url: str, body: bytes) -> None:
        """
        upload_blob uploads the body to the location specified by the URL.
        """
        fs, rpath = self._get_fs(url)
        with fs.open(rpath, "wb") as f:
            f.write(body)

    def download_file(self, url: str, path: str) -> None:
        """
        download_file downloads the file located at the URL to a location on disk.
        """
        fs, rpath = self._get_fs(url)
        fs.get_file(rpath, path)

    def upload_file(self, path: str, url: str) -> None:
        """
        upload_file uploads a file on disk to the location specified by the URL.
        """
        fs, rpath = self._get_fs(url)
        fs.put_file(path, rpath)


def _get_fsspec_provider(url: str) -> Optional[StorageProvider]:
    parsed = urlparse(url)
    scheme = parsed.scheme
    try:
        if get_filesystem_class(scheme):
            return FSSpecProvider(scheme)

        return None
    except (ValueError, ImportError):
        return None
