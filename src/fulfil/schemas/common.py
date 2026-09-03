from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Тело запроса/ответа — camelCase (конвенция DEV-PLAN.md)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
