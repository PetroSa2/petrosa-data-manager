"""
Base repository class for data access operations.
"""

import logging
from typing import Any

from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)


class BaseRepository:
    """
    Base repository providing common database access patterns.

    Subclasses should specify whether they use MySQL or MongoDB.
    """

    def __init__(
        self, mysql_adapter: MySQLAdapter | None, mongodb_adapter: MongoDBAdapter | None
    ):
        """
        Initialize repository with database adapters.

        Args:
            mysql_adapter: MySQL adapter for relational data
            mongodb_adapter: MongoDB adapter for time series data
        """
        self.mysql = mysql_adapter
        self.mongodb = mongodb_adapter

    def _model_to_dict(self, model: Any) -> dict:
        """
        Convert Pydantic model to dictionary.

        Args:
            model: Pydantic model instance

        Returns:
            Dictionary representation
        """
        if hasattr(model, "model_dump"):
            return model.model_dump()
        elif hasattr(model, "dict"):
            return model.dict()
        else:
            return dict(model)

    def _models_to_dicts(self, models: list[Any]) -> list[dict]:
        """
        Convert list of Pydantic models to list of dictionaries.

        Args:
            models: List of Pydantic model instances

        Returns:
            List of dictionary representations
        """
        return [self._model_to_dict(model) for model in models]
