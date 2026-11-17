import sys

sys.path.insert(0, "backend")

from groq import Groq

from app.core.config import get_settings

settings = get_settings()
models = Groq(api_key=settings.groq_api_key.get_secret_value()).models.list()
for model in models.data:
    if model.active:
        print(model.id)
