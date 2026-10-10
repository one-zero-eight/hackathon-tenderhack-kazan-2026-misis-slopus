from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

_DEFAULT_CATEGORY_PARQUETS = (
    DATA / "Инструменты_from_api.parquet",
    DATA / "Мебель_from_api.parquet",
    DATA / "Одежда_from_api.parquet",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Comma-separated paths; empty → default Инструменты / Мебель / Одежда.
    category_parquets: str = ""
    # Optional extra STE dump. Unset/empty → not loaded.
    ste_parquet: Path | None = None

    search_database: Path = ROOT / "search-web" / "data" / "search-index.sqlite"
    search_mmap_size: int = Field(default=2 * 1024**3, ge=0)
    search_cache_size_kib: int = Field(default=64 * 1024, gt=0)
    # Disk cache for portal images; default: <SEARCH_DATABASE>/../images
    image_cache_dir: Path | None = None
    # sku_id → float16[768]: .parquet (Git LFS) or LMDB dir; preferred over text meow cache
    sku_embeddings: Path = DATA / "sku_embeddings.parquet"
    meow_embed_url: str = "https://api.innohassle.ru/meow-embed"
    model_id: str = "google/embeddinggemma-2"
    dimensions: int = 768
    query_prompt: str = "task: search result | query: "
    document_prompt: str = ""
    index_schema_version: str = "parquet-index-v1"
    embed_batch_size: int = 256
    embed_concurrency: int = 4
    page_size: int = 24
    retrieval_limit: int = 240
    host: str = "127.0.0.1"
    port: int = 8070
    cors_origins: str = "http://127.0.0.1:3000,http://localhost:3000"
    typesafe_api_key: SecretStr | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-latest"
    typesafe_timeout: float = Field(default=120, gt=0)
    typesafe_confidence: float = Field(default=0.5, ge=0, le=1)

    @field_validator("ste_parquet", "image_cache_dir", mode="before")
    @classmethod
    def _empty_path_as_none(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    def catalog_parquet_paths(self) -> list[Path]:
        if self.category_parquets.strip():
            paths = [Path(part.strip()).expanduser() for part in self.category_parquets.split(",") if part.strip()]
        else:
            paths = list(_DEFAULT_CATEGORY_PARQUETS)
        if self.ste_parquet is not None:
            paths.append(Path(self.ste_parquet).expanduser())
        return paths

    @property
    def embedding_signature(self) -> str:
        return f"{self.model_id}:{self.dimensions}:search-result:document-v2"


settings = Settings()
