from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
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
    catalog_database: Path = ROOT / "search-web" / "data" / "catalog.sqlite"
    classifier_database: Path = DATA / "classifier-search.sqlite"
    ktru_sqlite: Path = DATA / "ktru-actual.sqlite"
    classifiers_sqlite: Path = DATA / "classifiers.sqlite"
    neighbor_score_threshold: float = Field(default=0.74, ge=-1, le=1)
    ktru_parquet: Path = DATA / "ktru-actual.parquet"
    moy_ktru_parquet: Path = DATA / "moy-zakupki-ktru.parquet"
    okpd2_parquet: Path = DATA / "okpd2-moy-zakupki.parquet"
    kpgz_parquet: Path = DATA / "pp-kpgz.parquet"
    spgz_parquet: Path = DATA / "spgz.parquet"
    catalog_mmap_size: int = Field(default=1024**3, ge=0)
    search_mmap_size: int = Field(default=2 * 1024**3, ge=0)
    search_cache_size_kib: int = Field(default=8 * 1024, gt=0)
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
    typesafe_confidence: float = Field(default=0.85, ge=0, le=1)
    openai_base_url: str = "https://api.innohassle.ru/llama/v1"
    openai_model: str = "ggml-org/gemma-4-E4B-it-GGUF:Q8_0"
    openai_timeout: float = Field(default=120, gt=0)
    jev_rerank_enabled: bool = True
    jev_rerank_limit: int = Field(default=25, ge=1, le=250)
    query_quality_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("query_quality_enabled", "QUERY_QUALITY"),
    )
    quality_reject: float = Field(default=0.3, ge=0, le=1)
    quality_doubt: float = Field(default=0.5, ge=0, le=1)
    quality_specific: float = Field(default=0.3, ge=0, le=1)

    @field_validator("ste_parquet", "image_cache_dir", mode="before")
    @classmethod
    def _empty_path_as_none(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("query_quality_enabled", mode="before")
    @classmethod
    def _parse_query_quality_enabled(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in ("off", "0", "false", "no"):
            return False
        if isinstance(value, str) and value.strip().lower() in ("on", "1", "true", "yes"):
            return True
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
