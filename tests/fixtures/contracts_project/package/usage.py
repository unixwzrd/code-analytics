from dataclasses import asdict

from .contracts import ArchivedJob, Request, Response


def build_response(payload: Request) -> Response:
    _ = payload
    return Response(accepted=True, message=None, request_id="cross-module")


def archive(record: ArchivedJob) -> ArchivedJob:
    return record


def serialize_response(response: Response) -> str:
    return response.model_dump_json()


def response_mapping(response: Response) -> dict[str, object]:
    return response.model_dump()


def archive_mapping(record: ArchivedJob) -> dict[str, object]:
    return asdict(record)


validated = Response.model_validate(
    {"accepted": True, "message": None, "request_id": "validated"}
)
validated_json = Response.model_validate_json(
    '{"accepted":true,"message":null,"request_id":"validated"}'
)
