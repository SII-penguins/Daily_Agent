"""Portable, offline test boundaries: no real accounts, models, or external HTTP.

Tests may provide their own transport mocks and temporary credential fixtures.
Missing transport mocks exercise the application's normal connection-error path.
"""
from pathlib import Path
import os
import subprocess
import httpx
import pytest

@pytest.fixture(autouse=True)
def offline_account_boundaries(monkeypatch, tmp_path_factory):
    from daily_agent import secrets
    temporary_base=tmp_path_factory.getbasetemp().resolve()
    def only_test_secret_files():
        explicit=os.environ.get('DAILY_AGENT_SECRETS_FILE','')
        candidates=[Path(value).expanduser() for value in explicit.split(os.pathsep) if value]
        if not explicit:
            home=Path.home()
            candidates=[home/'.daily-agent/secrets.toml',home/'.config/daily-agent/secrets.toml',home/'.cc-connect/config.toml']
        return [path for path in candidates if path.resolve().is_relative_to(temporary_base)]
    monkeypatch.setattr(secrets,'_secret_files',only_test_secret_files)
    for _,names in secrets.FULL_PROFILE_SECRET_GROUPS:
        for name in names: monkeypatch.delenv(name,raising=False)
    def no_external_http(self, request):
        raise httpx.ConnectError('Offline test: provide an explicit HTTP transport mock',request=request)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',no_external_http)
    original=subprocess.Popen
    def no_live_model(args,*pargs,**kwargs):
        executable=args[0] if isinstance(args,(list,tuple)) else args.split()[0]
        if Path(str(executable)).name.startswith(('codex','claude','cc-connect')):
            raise RuntimeError('Offline test blocked live model/delivery process')
        return original(args,*pargs,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',no_live_model)
