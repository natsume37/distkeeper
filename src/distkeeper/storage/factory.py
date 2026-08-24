"""Storage backend construction."""

from distkeeper.config import LocalStorageConfig, OssStorageConfig, StorageConfig
from distkeeper.storage.base import Storage
from distkeeper.storage.local import LocalStorage
from distkeeper.storage.oss import OssStorage


def create_storage(config: StorageConfig) -> Storage:
    if isinstance(config, LocalStorageConfig):
        return LocalStorage(config.root, config.prefix)
    if isinstance(config, OssStorageConfig):
        return OssStorage(config)
    raise AssertionError(f"unsupported storage configuration: {type(config).__name__}")
