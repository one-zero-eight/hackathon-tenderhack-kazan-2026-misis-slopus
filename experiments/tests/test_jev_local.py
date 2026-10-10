import importlib.util
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer

import pytest

from ktru_autofill.jev_local import UnsupportedQuestion, confidence, render
from ktru_autofill.system_one import sdk_caller

spec = importlib.util.spec_from_file_location(
    "jev9b_server", Path(__file__).resolve().parents[1] / "experiments" / "jev9b_server.py")
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)


def test_render_choice_keeps_labels_and_moves_descriptions_to_state():
    question = {"type": "choice", "instructions": {"question": "Какая категория?"},
                "criteria": {"ДИВАНЫ": {"раздел КПГЗ": "Мебель / Диваны"}, "СТОЛЫ": None}}
    prompt, options = render("choice", {"товар": "Диван"}, question)
    assert options == ["ДИВАНЫ", "СТОЛЫ"]
    assert prompt.startswith('[kind] choice\n[state] {"товар": "Диван"}\n\nOption descriptions:\n'
                             '{"ДИВАНЫ": {"раздел КПГЗ": "Мебель / Диваны"}}\n')
    assert prompt.endswith('[question] {"question": "Какая категория?"}\n[options]\nA) ДИВАНЫ\nB) СТОЛЫ\n[decision]:')


def test_render_limits():
    with pytest.raises(UnsupportedQuestion, match="1–16"):
        render("choice", "x", {"instructions": "?", "criteria": {str(i): None for i in range(17)}})
    with pytest.raises(UnsupportedQuestion, match="score"):
        render("score", "x", {"instructions": "?", "criteria": ["a"]})
    prompt, options = render("noul", "x", {"instructions": "Это диван?"})
    assert options == ["false", "true"] and prompt.endswith("[options]\nfalse\ntrue\n[decision]:")
    assert confidence([1., 0.]) == 1 and abs(confidence([.5, .5])) < 1e-9


class FakeModel:
    name = "fake/JEV"

    def system_one(self, body):
        q = body["questions"]["category"]
        if len(q["criteria"]) > 16:
            raise UnsupportedQuestion("много вариантов")
        labels = list(q["criteria"])
        probs = {label: (.7 if i == 1 else .3 / (len(labels) - 1)) for i, label in enumerate(labels)}
        return {"model": self.name, "usage": {"input_tokens": 10, "output_tokens": 1},
                "answers": {"category": {"type": "choice", "choice": labels[1], "confidence": .5,
                                         "probabilities": probs}}}


def test_typesafe_sdk_talks_to_local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), server_module.handler(FakeModel()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        call = sdk_caller(base_url=f"http://127.0.0.1:{server.server_port}", api_key="local")
        request = {"model": "fake/JEV", "state": {"товар": "Диван"},
                   "questions": {"category": {"type": "choice", "instructions": "?",
                                              "criteria": {"ДИВАНЫ": None, "СТОЛЫ": None}}}}
        response = call(request)
        assert response["model"] == "fake/JEV"
        assert response["answers"]["category"]["choice"] == "СТОЛЫ"
        assert response["answers"]["category"]["probabilities"] == {"ДИВАНЫ": .3, "СТОЛЫ": .7}
        assert response["usage"] == {"input_tokens": 10, "output_tokens": 1}
        json.dumps(response)
        from typesafe_sdk import TypeSafeAPIError
        too_many = {str(i): None for i in range(17)}
        with pytest.raises(TypeSafeAPIError):
            call(request | {"questions": {"category": {"type": "choice", "instructions": "?", "criteria": too_many}}})
    finally:
        server.shutdown()


def test_d1_question_keeps_label_and_json():
    from ktru_autofill.jev_local import d1_question
    q = d1_question({"type": "choice", "instructions": {"question": "Какая?"},
                     "criteria": {"ДИВАНЫ": {"раздел": "Мебель"}, "СТОЛЫ": None}})
    assert q["instructions"] == '{"question": "Какая?"}'
    assert q["criteria"] == {"ДИВАНЫ": 'ДИВАНЫ: {"раздел": "Мебель"}', "СТОЛЫ": None}


def test_nimble_schema_matches_decision_index_engine():
    from ktru_autofill.jev_local import Nimble
    body = {"state": {"товар": "Диван"}, "questions": {
        "c": {"type": "choice", "instructions": {"question": "Какая?"}, "criteria": {"ДИВАНЫ": {"раздел": "Мебель"}, "СТОЛЫ": None}},
        "n": {"type": "noul", "instructions": "Диван?", "criteria": {"true": "да", "false": None}}}}
    context, schema = Nimble.native_schema(body)
    assert context == '{"товар": "Диван"}'
    assert schema["c"] == {"description": '{"question": "Какая?"}', "type": "enum", "choices": ["ДИВАНЫ", "СТОЛЫ"],
                           "choice_descriptions": {"ДИВАНЫ": '{"раздел": "Мебель"}'}}
    assert schema["n"]["type"] == "boolean" and schema["n"]["choices"] == [False, True]
    with pytest.raises(UnsupportedQuestion):
        Nimble.native_schema({"state": "x", "questions": {"s": {"type": "score", "instructions": "?", "criteria": ["a"]}}})
