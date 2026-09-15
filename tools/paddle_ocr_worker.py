from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def _flatten_v2_output(result: Any) -> tuple[str, list[dict[str, Any]], float | None]:
    texts: list[str] = []
    lines: list[dict[str, Any]] = []
    scores: list[float] = []

    if not isinstance(result, list):
        return "", [], None

    pages = result
    if pages and isinstance(pages[0], list):
        candidates = pages
    else:
        candidates = [pages]

    for page in candidates:
        if not isinstance(page, list):
            continue
        for item in page:
            if not isinstance(item, list) or len(item) < 2:
                continue
            detail = item[1]
            if not isinstance(detail, (list, tuple)) or not detail:
                continue
            text = str(detail[0]).strip()
            score = None
            if len(detail) > 1:
                try:
                    score = float(detail[1])
                except (TypeError, ValueError):
                    score = None
            if text:
                texts.append(text)
                line = {"text": text}
                if score is not None:
                    line["score"] = score
                    scores.append(score)
                lines.append(line)

    confidence = round(sum(scores) / len(scores), 4) if scores else None
    return "\n".join(texts), lines, confidence


def _flatten_v3_output(result: Any) -> tuple[str, list[dict[str, Any]], float | None]:
    texts: list[str] = []
    lines: list[dict[str, Any]] = []
    scores: list[float] = []

    if isinstance(result, list):
        items = result
    else:
        items = [result]

    for item in items:
        if isinstance(item, dict):
            rec_texts = item.get("rec_texts") or []
            rec_scores = item.get("rec_scores") or []
            if rec_texts:
                for index, text in enumerate(rec_texts):
                    text_value = str(text).strip()
                    if not text_value:
                        continue
                    line: dict[str, Any] = {"text": text_value}
                    if index < len(rec_scores):
                        try:
                            score = float(rec_scores[index])
                        except (TypeError, ValueError):
                            score = None
                        if score is not None:
                            line["score"] = score
                            scores.append(score)
                    texts.append(text_value)
                    lines.append(line)

    confidence = round(sum(scores) / len(scores), 4) if scores else None
    return "\n".join(texts), lines, confidence


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        image_base64 = payload["image_base64"]
        lang = payload.get("lang") or "en"
        use_gpu = bool(payload.get("use_gpu", False))

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(base64.b64decode(image_base64))

        try:
            from paddleocr import PaddleOCR  # type: ignore

            ocr = _build_paddle_ocr(PaddleOCR, lang=lang, use_gpu=use_gpu)

            try:
                result = ocr.predict(str(temp_path))
                content, lines, confidence = _flatten_v3_output(result)
            except Exception:
                result = ocr.ocr(str(temp_path), cls=True)
                content, lines, confidence = _flatten_v2_output(result)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        print(
            json.dumps(
                {
                    "success": True,
                    "provider": "paddleocr",
                    "content": content,
                    "lines": lines,
                    "confidence": confidence,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def _build_paddle_ocr(paddle_cls: Any, *, lang: str, use_gpu: bool) -> Any:
    candidates = [
        {
            "lang": lang,
            "use_gpu": use_gpu,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
        },
        {
            "lang": lang,
            "use_gpu": use_gpu,
            "use_angle_cls": True,
        },
        {
            "lang": lang,
            "use_gpu": use_gpu,
        },
        {
            "lang": lang,
        },
    ]

    last_error: Exception | None = None
    for kwargs in candidates:
        try:
            return paddle_cls(**kwargs)
        except TypeError as exc:
            last_error = exc
            message = str(exc)
            if "use_gpu" not in message and "use_doc_orientation_classify" not in message and "use_doc_unwarping" not in message and "use_angle_cls" not in message:
                raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("Failed to initialize PaddleOCR.")


if __name__ == "__main__":
    raise SystemExit(main())
