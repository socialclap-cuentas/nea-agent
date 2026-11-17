"""Un audio vacío (≈0 s) no se transcribe: transcribirlo inventa texto sobre el silencio."""
from types import SimpleNamespace

from app import media
from app.state import InboundMessage


class _Crm:
    def __init__(self, data: bytes):
        self._data = data

    async def get_media(self, media_id):
        return self._data, "audio/ogg; codecs=opus"


class _Llm:
    def __init__(self):
        self.llamado = False

    async def transcribe(self, data, mime, filename="audio.ogg"):
        self.llamado = True
        return "texto"


def _msg():
    return InboundMessage(
        wa_message_id="w1", identity="549", type="audio",
        media_id="m1", media_mime="audio/ogg", media_voice=True,
    )


async def test_audio_diminuto_es_un_no_pude_oirlo_y_no_se_transcribe():
    llm = _Llm()
    ctx = SimpleNamespace(crm=_Crm(b"\x00" * 40), llm=llm)
    parte = await media._audio(ctx, _msg())
    assert not llm.llamado
    assert "NO pudiste" in parte.text


async def test_audio_normal_se_transcribe():
    llm = _Llm()
    ctx = SimpleNamespace(crm=_Crm(b"\x00" * 8000), llm=llm)
    parte = await media._audio(ctx, _msg())
    assert llm.llamado
    assert "transcrita" in parte.text
