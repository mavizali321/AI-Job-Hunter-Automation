from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./jobhunter.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change-me"
    dashboard_password: str = "change-me"

    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_verify_token: str = "change-me"
    whatsapp_recipient: str = ""
    whatsapp_app_secret: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    worker_token: str = "change-me"
    browser_profile_dir: str = "./browser_profile"
    api_base_url: str = "http://127.0.0.1:8000"
    worker_poll_seconds: int = 15
    applications_dir: str = "./applications"

    search_window_days: int = 7
    approval_threshold: int = 80
    review_threshold: int = 70
    timezone: str = "Asia/Karachi"

    greenhouse_board_tokens: str = ""
    lever_company_slugs: str = ""
    ashby_company_slugs: str = ""
    career_page_urls: str = ""

    serper_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_api_key: str = ""

    job_titles: str = ""
    job_locations: str = "Karachi,Islamabad,Lahore,Pakistan,Remote"
    max_results_per_run: int = 100
    min_profile_match_score: int = 70

    resume_checksum: str = "E47DD0E6E27E3FE906630219A1CA44D450BE5DC2F07186FF8637D17CCE59DD4E"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    def _split_csv(self, val: str) -> list[str]:
        return [v.strip() for v in val.split(",") if v.strip()] if val else []

    @property
    def greenhouse_tokens_list(self) -> list[str]:
        return self._split_csv(self.greenhouse_board_tokens)

    @property
    def lever_slugs_list(self) -> list[str]:
        return self._split_csv(self.lever_company_slugs)

    @property
    def ashby_slugs_list(self) -> list[str]:
        return self._split_csv(self.ashby_company_slugs)

    @property
    def career_urls_list(self) -> list[str]:
        return self._split_csv(self.career_page_urls)

    @property
    def job_titles_list(self) -> list[str]:
        custom = self._split_csv(self.job_titles)
        if custom:
            return custom
        return [
            "AI Engineer", "Junior AI Engineer", "Applied AI Engineer",
            "LLM Engineer", "AI Automation Engineer", "Python Developer",
            "Software Engineer", "SAP ABAP Developer", "Junior Technical Consultant",
        ]

    @property
    def job_locations_list(self) -> list[str]:
        return self._split_csv(self.job_locations)


settings = Settings()
