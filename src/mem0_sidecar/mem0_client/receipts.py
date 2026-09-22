from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class AddReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    mutation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    status: Literal["RUNNING", "SUCCEEDED", "FAILED"]
    result: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def require_success_result(self) -> "AddReceipt":
        if self.status == "SUCCEEDED" and self.result is None:
            raise ValueError("Successful add receipt requires its original result")
        return self
