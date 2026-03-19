# Petrosa Data Manager Contracts

## Data Insertion Contract Updates

### MySQL Adapter (`id` generation)
When inserting records via the `MySQLAdapter`, if a record is missing the required `id` field (which is the primary key for many tables like `klines_m5`), the adapter now automatically generates a `uuid.uuid4()` string to ensure the insert succeeds without raising `SAWarning` or failing silently.

### Generic Inserts (`GenericModel`)
The generic insert endpoints (`/api/v1/data/insert`) utilize a dynamic `GenericModel`. This model has been updated with `model_config = ConfigDict(extra="allow")` to ensure that any extra payload fields are not dropped during serialization before being written to the database. This allows the extractor to pass arbitrary payloads that conform to the target collection's schema.

### MongoDB Adapter (Timestamp parsing)
The `MongoDBAdapter` expects a `timestamp` field. If this field is an ISO formatted string, it automatically parses it into a `datetime` object. The logic has been made robust to handle `Z` suffixes and prevent double-offsetting (e.g., `+00:00+00:00`), ensuring successful insertion via `fromisoformat`.