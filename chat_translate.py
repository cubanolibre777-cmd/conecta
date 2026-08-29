# -*- coding: utf-8 -*-
"""
Traducción automática de mensajes de chat usando `deep-translator`
(motor de Google Translate, sin necesidad de API key ni cuenta de pago).

Instalación:
    pip install deep-translator --break-system-packages

No requiere ninguna variable de entorno ni configuración adicional.

Importante: esta librería usa el mismo backend web que usa cualquier
persona al traducir en translate.google.com, sin pasar por la API oficial
de pago. Es gratis y no tiene límite de caracteres, pero:
- No es un servicio oficial de Google, así que no hay garantía de que siga
  funcionando para siempre (si Google cambia algo en su web, puede fallar
  temporalmente hasta que la librería se actualice).
- Todo el código de abajo está preparado para que, si la traducción falla,
  el chat simplemente muestre el mensaje original sin traducir — nunca
  rompe la conversación.
- Si en el futuro Conecta crece y esto se vuelve poco confiable, migrar a
  la API oficial de pago de Google (o a Microsoft Translator) es tan
  simple como reemplazar la función `translate_text` de este archivo; el
  resto de la app (app.py) no cambia nada.
"""

from deep_translator import GoogleTranslator
from deep_translator.exceptions import LanguageNotSupportedException, NotValidPayload

# deep-translator usa "zh-CN" para chino simplificado, mientras que el
# resto de nuestra app usa el código corto "zh" (ver translations.py).
# Este mapa traduce nuestros códigos a los que espera la librería.
_LANG_CODE_MAP = {
    "zh": "zh-CN",
}

# Caché simple en memoria: evita traducir el mismo texto+idioma dos veces
# durante la vida del proceso (ahorra llamadas y latencia).
_translation_cache = {}


def _resolve_lang_code(lang):
    return _LANG_CODE_MAP.get(lang, lang)


def is_translation_enabled():
    # Siempre disponible: no depende de ninguna clave configurada.
    return True


def translate_text(text, target_lang, source_lang=None):
    """Traduce `text` al idioma `target_lang`.

    Devuelve un dict: {"translated": str, "detected_source": str|None, "ok": bool}
    Si la traducción falla por cualquier motivo, devuelve el texto original
    sin traducir (ok=False) para que el chat nunca se rompa por esto.
    """
    if not text or not text.strip():
        return {"translated": text, "detected_source": source_lang, "ok": True}

    cache_key = (text, target_lang, source_lang)
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    src = _resolve_lang_code(source_lang) if source_lang else "auto"
    tgt = _resolve_lang_code(target_lang)

    try:
        translated_text = GoogleTranslator(source=src, target=tgt).translate(text)
        if not translated_text:
            raise ValueError("Respuesta vacía del traductor")
        result = {
            "translated": translated_text,
            "detected_source": source_lang,
            "ok": True,
        }
    except (LanguageNotSupportedException, NotValidPayload) as e:
        print(f"[chat_translate] Idioma o texto no válido: {e}")
        result = {"translated": text, "detected_source": source_lang, "ok": False}
    except Exception as e:
        # Cubre errores de red, timeouts, o cambios en el backend de Google.
        print(f"[chat_translate] Error al traducir: {e}")
        result = {"translated": text, "detected_source": source_lang, "ok": False}

    _translation_cache[cache_key] = result
    return result


def maybe_translate_message(content, sender_lang, receiver_lang):
    """Traduce un mensaje de chat solo si el idioma del emisor y el receptor
    son distintos. Si son iguales, no gasta una traducción innecesaria.

    Devuelve dict: {"original": str, "translated": str|None, "was_translated": bool}
    """
    if not content or not content.strip():
        return {"original": content, "translated": None, "was_translated": False}

    if not sender_lang or not receiver_lang or sender_lang == receiver_lang:
        return {"original": content, "translated": None, "was_translated": False}

    result = translate_text(content, target_lang=receiver_lang, source_lang=sender_lang)

    if not result["ok"] or result["translated"].strip() == content.strip():
        return {"original": content, "translated": None, "was_translated": False}

    return {"original": content, "translated": result["translated"], "was_translated": True}
