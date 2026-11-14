"""Concise question presentation shared by the evidence UI and Word export."""
import re


def short_sentence(value, limit=60):
    """Keep a single sentence in legacy display text; never alter saved reports."""
    text = re.split(r"(?<=[。！？!?])|[\r\n]+", value.strip(), maxsplit=1)[0].strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip("，、；,; ") + "…"


def question_parts(value):
    for part in re.findall(r"[^？?]+[？?]", value):
        # Older reports sometimes prefix a question with explanatory sentences.
        question = re.split(r"[。！!\r\n]", part)[-1].strip()
        question = re.sub(r"^(?:\d+[.、]\s*|[-•]\s*|(?:核验问题|问题)[：:]\s*)", "", question)
        if question.rstrip("？?").strip():
            yield question


def question_details(values, items=()):
    reasons = {}
    key_for = lambda text: re.sub(r"\s+", "", text).rstrip("？?")
    for item in items:
        for question in question_parts(item.get("verification_question", "")):
            reasons.setdefault(key_for(question), item.get("why_matters") or item.get("why_needed", ""))
    questions, seen = [], set()
    for value in values:
        raw = value.get("question", "") if isinstance(value, dict) else value
        reason = value.get("why_matters", "") if isinstance(value, dict) else ""
        for question in question_parts(raw):
            key = key_for(question)
            if key in seen:
                continue
            seen.add(key)
            if len(question) > 60:
                question = question[:58].rstrip("，、；,; ") + "…？"
            questions.append({"question": question, "why_matters": short_sentence(
                reason or reasons.get(key) or "明确后续需要核对或补充的材料。")})
            if len(questions) == 10:
                return questions
    return questions


def bounded_questions(values):
    return [item["question"] for item in question_details(values)]
