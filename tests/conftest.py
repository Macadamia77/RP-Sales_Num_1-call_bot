import os
import tempfile

# callbot.app 모듈이 import 시점에 저장소를 만들므로, 테스트용 임시 폴더를 먼저 지정한다.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="callbot-test-")
os.environ.setdefault("LLM_PROVIDER", "rule")
os.environ.setdefault("TTS_PROVIDER", "mock")
