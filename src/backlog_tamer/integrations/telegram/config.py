from pydantic import SecretStr
from pydantic_settings import BaseSettings


class TelegramConfig(BaseSettings):
    bot_token: SecretStr
    allowed_user_id: int
