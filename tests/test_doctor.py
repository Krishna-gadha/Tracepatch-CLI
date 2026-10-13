from tracepatch import doctor
from tracepatch.cli import main


def test_doctor_reports_ready_when_the_model_is_installed(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TRACEPATCH_LOCAL_MODEL", raising=False)
    monkeypatch.setattr(doctor, "installed_local_models", lambda: ["llama3.2:latest"])
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "model llama3.2 is installed" in out and "Ready." in out


def test_doctor_says_how_to_get_the_model(monkeypatch, capsys):
    monkeypatch.delenv("TRACEPATCH_LOCAL_MODEL", raising=False)
    monkeypatch.setattr(doctor, "installed_local_models", lambda: [])
    assert main(["doctor"]) == 0          # the model is optional: still usable
    assert "ollama pull llama3.2" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "installed_local_models", lambda: None)
    main(["doctor"])
    assert "Ollama is not running" in capsys.readouterr().out
