from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PLINK_WEBHOOK_SECRET: str = ""
    INMOBI_API_KEY: str = ""  # optional — InMobi publisher oRTB authenticates via Publisher ID
    INMOBI_ENDPOINT: str = "https://api.w.inmobi.com/ortb"
    INMOBI_PUBLISHER_ID: str = ""
    INMOBI_PLACEMENT_ID: str = ""
    LOG_LEVEL: str = "INFO"
    ENV: str = "development"

    @property
    def is_development(self) -> bool:
        return self.ENV.lower() == "development"

    @property
    def sandbox_mode(self) -> bool:
        """Sandbox when ENV != production OR Publisher ID is not set."""
        return self.is_development and not self.INMOBI_PUBLISHER_ID


settings = Settings()
