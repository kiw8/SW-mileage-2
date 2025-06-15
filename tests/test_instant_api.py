# 필요한 라이브러리 임포트
import json
import re
from dataclasses import dataclass  # 데이터 클래스를 간편하게 정의할 수 있도록 지원
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonrpcclient.client  # JSON-RPC 클라이언트 기능 제공
import pytest  # 파이썬 테스트 프레임워크
from flask import Flask  # Flask 웹 프레임워크
from instant_client import InstantClient  # JSON-RPC 요청을 래핑해주는 클라이언트
from jsonrpc.exceptions import JSONRPCDispatchException  # JSON-RPC 정의된 예외
from jsonrpcclient import Response
from jsonrpcclient.exceptions import ReceivedErrorResponseError  # 응답에서 에러가 발생했을 경우 예외
from instant_api import InstantAPI, InstantError  # 사용자 정의 API 서버 및 에러 클래스

# Flask 애플리케이션 생성
app = Flask(__name__)
folder = Path(__file__).parent  # 현재 파일의 디렉토리 경로

# 데이터 전송 객체 정의: Point(x, y)
@dataclass
class Point:
    x: int
    y: int

# Flask 앱에 JSON-RPC API 등록
api = InstantAPI(app)

# 실제 API 로직이 정의되는 클래스
@api(swagger_view_attrs=dict(tags=["Point methods"]))
class Methods:
    def translate(self, p: Point, dx: int, dy: int) -> Point:
        """
        주어진 Point 객체를 dx, dy 만큼 평행 이동시킴.
        dy 값에 따라 다양한 예외 상황을 유도하여 테스트 시나리오 구성.
        """
        if dy == -8:
            # 일반적인 파이썬 예외: 서버 내부 처리 에러 상황 시뮬레이션
            raise ValueError("dy == -8: Unhandled ValueError")
        if dy == -9:
            # InstantAPI 고유 예외를 발생시켜 커스텀 에러 응답 테스트
            raise InstantError(
                code=12345,
                message="This is an instant message",
                data={"foo": 123},
                http_code=401,
            )
        if dy == -10:
            # JSON-RPC 프로토콜 정의된 예외 발생: 클라이언트가 해당 코드 처리 가능
            raise JSONRPCDispatchException(
                code=45678,
                message="This is a JSON RPC message",
                data={"foo": 456},
            )
        return Point(p.x + dx, p.y + dy)  # 정상적인 이동 결과 반환

# Flask 테스트용 클라이언트 설정
app.config['TESTING'] = True
flask_client = app.test_client()

# 테스트용 JSON-RPC 클라이언트 정의 (Flask 클라이언트 감싸기)
class _TestJsonRpcClient(jsonrpcclient.client.Client):
    def __init__(self, test_client, endpoint):
        super().__init__()
        self.test_client = test_client
        self.endpoint = endpoint

    def send_message(self, request: str, response_expected: bool, **kwargs: Any) -> Response:
        # Flask 내부 요청을 통해 JSON-RPC 호출 시뮬레이션
        response = self.test_client.post(self.endpoint, data=request.encode())
        return Response(response.data.decode("utf8"), raw=response)

# 클라이언트 및 메서드 래핑
rpc_client = _TestJsonRpcClient(flask_client, "/api/")
client_methods = InstantClient(rpc_client, Methods()).methods

# 단순 동작 확인 테스트: 클래스 직접 호출 vs RPC 클라이언트 호출 결과 비교
def test_simple():
    for methods in [client_methods, Methods()]:
        assert methods.translate(Point(1, 2), 3, 4) == Point(4, 6)

# Flask POST 요청 헬퍼 함수
def flask_post(url, data):
    response = flask_client.post(url, data=json.dumps(data).encode())
    return response, json.loads(response.data.decode())

# API 직접 호출 경로 테스트: 정상 응답 확인
def test_method_path():
    response, data = flask_post("/api/translate", {"p": {"x": 1, "y": 2}, "dx": 3, "dy": 4})
    assert response.status_code == 200
    assert data == {
        "id": None,
        "jsonrpc": "2.0",
        "result": {"x": 4, "y": 6},
    }

# 파라미터 누락에 따른 타입 에러 테스트
def test_server_type_error():
    message = "TypeError: missing a required argument: 'dy'"
    with pytest.raises(ReceivedErrorResponseError, match=message):
        rpc_client.translate(1, 3)  # dy 없음

    # JSON-RPC 직접 호출
    response, data = flask_post("/api/translate", {"p": 1, "dx": 3})
    assert response.status_code == 400
    assert data == {
        "error": {"code": -32602, "data": None, "message": message},
        "id": None,
        "jsonrpc": "2.0",
    }

# 데이터 타입이 잘못된 경우 검증 에러 테스트
def test_server_validation_error():
    message = "marshmallow.exceptions.ValidationError: {'p': {'_schema': ['Invalid input type.']}}"
    with pytest.raises(ReceivedErrorResponseError, match=re.escape(message)):
        rpc_client.translate("asd", 3, 4)  # p는 Point 객체여야 함

    response, data = flask_post("/api/translate", {"p": "asd", "dx": 3, "dy": 4})
    assert response.status_code == 400
    assert data == {
        "error": {
            "code": -32602,
            "data": {"p": {"_schema": ["Invalid input type."]}},
            "message": message,
        },
        "id": None,
        "jsonrpc": "2.0",
    }

# InstantError 에러 시나리오 확인 (401 반환 확인)
def test_instant_error():
    response, data = flask_post("/api/translate", {"p": {"x": 1, "y": 2}, "dx": 3, "dy": -9})
    assert response.status_code == 401
    assert data["error"]["message"] == "This is an instant message"

# JSON-RPC DispatchException 예외 처리 확인
def test_jsonrpc_dispatch_exception():
    response, data = flask_post("/api/translate", {"p": {"x": 1, "y": 2}, "dx": 3, "dy": -10})
    assert response.status_code == 500
    assert data["error"]["message"] == "This is a JSON RPC message"

# 미처리 예외 상황 (ValueError) 테스트
def test_unhandled_error():
    response, data = flask_post("/api/translate", {"p": {"x": 1, "y": 2}, "dx": 3, "dy": -8})
    assert response.status_code == 500
    assert data["error"]["message"] == "Unhandled error in method translate"

# 잘못된 JSON 문자열이 전송된 경우 처리 테스트
def test_invalid_json():
    def check(path, expected_status_code):
        response = flask_client.post(path, data="foo")  # 유효하지 않은 JSON
        assert response.status_code == expected_status_code
        assert json.loads(response.data.decode()) == {
            "error": {"code": -32700, "message": "Parse error"},
            "id": None,
            "jsonrpc": "2.0",
        }

    check("/api/", 200)  # 루트 경로
    check("/api/translate", 400)  # 메서드 경로

# 존재하지 않는 메서드 호출 테스트
def test_method_not_found():
    with pytest.raises(ReceivedErrorResponseError, match="Method not found"):
        rpc_client.do_thing(1, 3)

# 인증 실패 시 처리 테스트
def test_auth_error():
    with patch.object(InstantAPI, "is_authenticated", lambda self: False):
        response = flask_client.post("/api/")
        assert response.status_code == 403
        assert response.data == b"Forbidden"

# Notification 형태 요청 처리 (응답 없음)
def test_notification():
    rpc_client.notify("translate", 234)

# API 명세 파일(JSON 형식)과 일치 여부 테스트
def test_apispec():
    response = flask_client.get("/apispec_1.json")
    with open(folder / "apispec.json", "r") as f:
        expected = json.load(f)
    assert response.json == expected

# 이 코드는 Flask 기반 JSON-RPC API 서버와 그에 대한 테스트 코드입니다.
# 특히 InstantAPI와 InstantClient라는 구조를 활용해 JSON-RPC 방식으로 메서드를 호출하고,
# 다양한 예외 상황에 대한 테스트를 진행하고 있습니다