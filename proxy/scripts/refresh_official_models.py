#!/usr/bin/env python3
"""Refresh Codex unified model catalog from official OpenAI and DeepSeek docs.

This script intentionally depends only on the Python standard library.  It
fetches public official docs, extracts stable model names and visible capacity
parameters, and writes ~/.codex/model-catalogs/unified.json.  If a fetch or parse
fails, it falls back to the last documented stable values embedded below.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

OPENAI_LATEST_MODEL_URL = "https://developers.openai.com/api/docs/guides/latest-model.md"
OPENAI_MODEL_URLS = {
    "gpt-5.4-mini": "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
    "gpt-5.4": "https://developers.openai.com/api/docs/models/gpt-5.4",
    "gpt-5.5": "https://developers.openai.com/api/docs/models/gpt-5.5",
    "gpt-5.6-sol": "https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    "gpt-5.6-terra": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
    "gpt-5.6-luna": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
}
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
DEEPSEEK_CHAT_API_URL = "https://api-docs.deepseek.com/api/create-chat-completion"

FALLBACK_GPT56 = {
    "context_window": 1_050_000,
    "max_completion_tokens": 128_000,
    "knowledge_cutoff": "Feb 16, 2026",
    "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
    "default_reasoning": "medium",
}
FALLBACK_OPENAI = {
    "gpt-5.4-mini": {"context_window": 400_000, "max_completion_tokens": 128_000, "knowledge_cutoff": "Aug 31, 2025", "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"], "default_reasoning": "none"},
    "gpt-5.4": {"context_window": 1_050_000, "max_completion_tokens": 128_000, "knowledge_cutoff": "Aug 31, 2025", "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"], "default_reasoning": "none"},
    "gpt-5.5": {"context_window": 1_050_000, "max_completion_tokens": 128_000, "knowledge_cutoff": "Dec 01, 2025", "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"], "default_reasoning": "medium"},
    "gpt-5.6-sol": FALLBACK_GPT56,
    "gpt-5.6-terra": FALLBACK_GPT56,
    "gpt-5.6-luna": FALLBACK_GPT56,
}
FALLBACK_DEEPSEEK_V4 = {
    "context_window": 1_000_000,
    "max_completion_tokens": 384_000,
    "reasoning_efforts": ["high", "max"],
    "default_reasoning": "high",
    "thinking_default": "enabled",
}


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "codex-official-model-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def text_from_html(raw: str) -> str:
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
    raw = re.sub(r"<!--[\s\S]*?-->", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def parse_int_token(s: str) -> int:
    s = s.strip().replace(",", "")
    m = re.match(r"([0-9.]+)\s*([kKmM]?)", s)
    if not m:
        raise ValueError(f"cannot parse token count: {s!r}")
    value = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def display_name_from_slug(slug: str) -> str:
    if slug.startswith("gpt-"):
        parts = slug.split("-")
        if len(parts) <= 2:
            return slug.upper()
        return f"GPT-{parts[1]} " + " ".join(part.capitalize() for part in parts[2:])
    if slug.startswith("deepseek-"):
        parts = slug.split("-")
        return "DeepSeek " + " ".join(part.upper() if part.startswith("v") else part.capitalize() for part in parts[1:])
    return slug


def discover_openai_latest_models(raw: str) -> tuple[str, str, list[str]]:
    """Discover the latest stable GPT family and concrete variants from the guide."""
    match = re.search(r"^\s*model:\s*(gpt-\d+\.\d+(?:-[a-z0-9-]+)?)\s*$", raw, re.I | re.M)
    latest_model = match.group(1).lower() if match else "gpt-5.6-sol"
    family_match = re.match(r"(gpt-\d+\.\d+)", latest_model)
    latest_alias = family_match.group(1) if family_match else "gpt-5.6"
    variants: list[str] = []
    for slug in re.findall(r"`(gpt-\d+\.\d+(?:-[a-z0-9-]+)?)`", raw, re.I):
        slug = slug.lower()
        if slug.startswith(latest_alias + "-") and slug not in variants:
            variants.append(slug)
    if latest_model != latest_alias and latest_model not in variants:
        variants.insert(0, latest_model)
    elif latest_model in variants:
        variants.remove(latest_model)
        variants.insert(0, latest_model)
    if not variants:
        variants = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    return latest_model, latest_alias, variants


def discover_deepseek_stable_models(raw: str) -> tuple[str, str]:
    """Discover the newest official Pro and Flash stable IDs from pricing docs."""
    slugs = {
        slug.lower()
        for slug in re.findall(r"deepseek-v\d+(?:\.\d+)?-(?:pro|flash)", raw, re.I)
    }

    def version_key(slug: str) -> tuple[int, ...]:
        match = re.search(r"-v(\d+(?:\.\d+)*)-", slug)
        return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)

    pro = max((slug for slug in slugs if slug.endswith("-pro")), key=version_key, default="deepseek-v4-pro")
    flash = max((slug for slug in slugs if slug.endswith("-flash")), key=version_key, default="deepseek-v4-flash")
    return pro, flash


def parse_openai_model_page(slug: str, raw: str) -> dict:
    text = text_from_html(raw)
    ctx = FALLBACK_GPT56["context_window"]
    out = FALLBACK_GPT56["max_completion_tokens"]
    cutoff = FALLBACK_GPT56["knowledge_cutoff"]
    m = re.search(r"([0-9][0-9,]*)\s+context window", text, re.I)
    if m:
        ctx = parse_int_token(m.group(1))
    m = re.search(r"([0-9][0-9,]*)\s+max output tokens", text, re.I)
    if m:
        out = parse_int_token(m.group(1))
    m = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s+knowledge cutoff", text)
    if m:
        cutoff = m.group(1)
    desc = {
        "gpt-5.4-mini": "GPT-5.4 Mini efficient model for high-volume workloads",
        "gpt-5.4": "GPT-5.4 frontier model for complex professional work",
        "gpt-5.5": "GPT-5.5 frontier model for complex professional work",
        "gpt-5.6-sol": "Frontier GPT-5.6 model for complex professional work",
        "gpt-5.6-terra": "GPT-5.6 model balancing intelligence and cost",
        "gpt-5.6-luna": "GPT-5.6 model optimized for cost-sensitive high-volume workloads",
    }.get(slug, f"{display_name_from_slug(slug)} official stable model")
    efforts = list(FALLBACK_OPENAI.get(slug, FALLBACK_GPT56)["reasoning_efforts"])
    default_reasoning = FALLBACK_OPENAI.get(slug, FALLBACK_GPT56)["default_reasoning"]
    m = re.search(r"Reasoning\.effort supports:\s*([^\.]+)\.", text, re.I)
    if m:
        phrase = m.group(1).replace("and", ",")
        parsed = []
        default = None
        for part in phrase.split(","):
            part = part.strip()
            vm = re.match(r"(none|low|medium|high|xhigh|max)(?:\s*\(default\))?", part)
            if vm:
                val = vm.group(1)
                parsed.append(val)
                if "default" in part:
                    default = val
        if parsed:
            efforts = parsed
        if default:
            default_reasoning = default
    return {
        "slug": slug,
        "context_window": ctx,
        "max_completion_tokens": out,
        "knowledge_cutoff": cutoff,
        "description": desc,
        "reasoning_efforts": efforts,
        "default_reasoning": default_reasoning,
    }


def parse_deepseek_docs(pricing_raw: str, api_raw: str) -> dict:
    pricing = text_from_html(pricing_raw)
    api = text_from_html(api_raw)
    ctx = FALLBACK_DEEPSEEK_V4["context_window"]
    out = FALLBACK_DEEPSEEK_V4["max_completion_tokens"]
    m = re.search(r"CONTEXT LENGTH\s+([0-9.]+\s*[KM]?)", pricing, re.I)
    if m:
        ctx = parse_int_token(m.group(1))
    m = re.search(r"MAX OUTPUT\s+MAXIMUM:\s*([0-9.]+\s*[KM]?)", pricing, re.I)
    if m:
        out = parse_int_token(m.group(1))
    efforts = FALLBACK_DEEPSEEK_V4["reasoning_efforts"]
    m = re.search(r"reasoning_effort string Possible values:\s*\[\s*([^\]]+)\]", api, re.I)
    if m:
        values = [x.strip().strip(",") for x in re.split(r"[, ]+", m.group(1)) if x.strip().strip(",")]
        # The docs expose high/max; ignore prose words if the HTML text parser over-captures.
        values = [x for x in values if x in {"high", "max"}]
        if values:
            efforts = values
    return {
        "context_window": ctx,
        "max_completion_tokens": out,
        "reasoning_efforts": efforts,
        "default_reasoning": "high",
        "thinking_default": "enabled",
    }


def reasoning_levels(efforts: list[str], family: str) -> list[dict]:
    labels = {
        "none": "No reasoning; latency baseline where supported",
        "low": "Efficient reasoning with modest latency",
        "medium": "Official balanced default for GPT-5.6",
        "high": "Deep reasoning for complex tasks",
        "xhigh": "Extra-high reasoning for long or difficult agentic tasks",
        "max": "Maximum reasoning for hardest quality-first workloads",
    }
    if family == "deepseek":
        labels.update({
            "high": "DeepSeek official default reasoning effort",
            "max": "DeepSeek maximum reasoning effort",
        })
    return [{"effort": e, "description": labels.get(e, e)} for e in efforts]


def gpt_entry(slug: str, display: str, description: str, priority: int, meta: dict, alias_target: str | None = None) -> dict:
    return {
        "slug": slug,
        "display_name": display,
        "description": description,
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "context_window": meta["context_window"],
        "max_context_window": meta["context_window"],
        "effective_context_window_percent": 95,
        "auto_compact_token_limit": int(meta["context_window"] * 0.75),
        "max_completion_tokens": meta["max_completion_tokens"],
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supports_image_detail_original": True,
        "supports_parallel_tool_calls": True,
        "supports_search_tool": False,
        "web_search_tool_type": "text_and_image",
        "apply_patch_tool_type": "freeform",
        "shell_type": "shell_command",
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "auto",
        "default_reasoning_level": meta.get("default_reasoning", FALLBACK_GPT56["default_reasoning"]),
        "supported_reasoning_levels": reasoning_levels(meta.get("reasoning_efforts", FALLBACK_GPT56["reasoning_efforts"]), "gpt"),
        "support_verbosity": False,
        "default_verbosity": "medium",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "base_instructions": f"You are a coding agent powered by {display}.",
        "cost": {"input": 0, "output": 0},
        "release_date": "2026-07-10",
        "last_updated": dt.date.today().isoformat(),
        "knowledge_cutoff": meta.get("knowledge_cutoff"),
        "structured_output": True,
        "open_weights": False,
        "attachment": True,
        "experimental_supported_tools": [],
        "additional_speed_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "provider_defaults": {
            "api": "responses",
            "reasoning": {"effort": meta.get("default_reasoning", FALLBACK_GPT56["default_reasoning"])},
            **({"alias_target": alias_target} if alias_target else {}),
        },
    }


def deepseek_entry(slug: str, display: str, description: str, priority: int, meta: dict) -> dict:
    return {
        "slug": slug,
        "display_name": display,
        "description": description,
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "context_window": meta["context_window"],
        "max_context_window": meta["context_window"],
        "effective_context_window_percent": 95,
        "auto_compact_token_limit": int(meta["context_window"] * 0.75),
        "max_completion_tokens": meta["max_completion_tokens"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supports_image_detail_original": False,
        "supports_parallel_tool_calls": True,
        "supports_search_tool": False,
        "web_search_tool_type": "text_and_image",
        "apply_patch_tool_type": "freeform",
        "shell_type": "shell_command",
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "auto",
        "default_reasoning_level": meta["default_reasoning"],
        "supported_reasoning_levels": reasoning_levels(meta["reasoning_efforts"], "deepseek"),
        "support_verbosity": False,
        "default_verbosity": "medium",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "base_instructions": f"You are a coding agent powered by {display}.",
        "cost": {"input": 0, "output": 0},
        "release_date": "2026-07-10",
        "last_updated": dt.date.today().isoformat(),
        "structured_output": True,
        "open_weights": False,
        "attachment": False,
        "experimental_supported_tools": [],
        "additional_speed_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "provider_defaults": {
            "api": "chat.completions",
            "base_url": "https://api.deepseek.com",
            "thinking": {"type": meta["thinking_default"]},
            "reasoning_effort": meta["default_reasoning"],
            "temperature": 1,
            "top_p": 1,
            "ignored_in_thinking_mode": ["temperature", "top_p", "presence_penalty", "frequency_penalty"],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(Path.home()/".codex/model-catalogs/unified.json"))
    ap.add_argument("--stamp", default=str(Path.home()/".codex/model-catalogs/.official-refresh.stamp"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    fetched = []
    openai_model_urls = dict(OPENAI_MODEL_URLS)
    try:
        latest = fetch(OPENAI_LATEST_MODEL_URL)
        fetched.append(OPENAI_LATEST_MODEL_URL)
        latest_model, latest_alias, latest_variants = discover_openai_latest_models(latest)
        for slug in latest_variants:
            openai_model_urls.setdefault(
                slug,
                f"https://developers.openai.com/api/docs/models/{slug}",
            )
    except Exception as e:
        latest_model = "gpt-5.6-sol"
        latest_alias = "gpt-5.6"
        latest_variants = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        if not args.quiet:
            print(f"warn: OpenAI latest fetch failed: {e}", file=sys.stderr)
    gpt_meta = {
        slug: {**FALLBACK_OPENAI.get(slug, FALLBACK_GPT56), "slug": slug}
        for slug in openai_model_urls
    }
    for slug, url in openai_model_urls.items():
        try:
            gpt_meta[slug] = {**gpt_meta[slug], **parse_openai_model_page(slug, fetch(url))}
            fetched.append(url)
        except Exception as e:
            if not args.quiet:
                print(f"warn: OpenAI model fetch failed for {slug}: {e}", file=sys.stderr)
            fb = FALLBACK_OPENAI.get(slug, FALLBACK_GPT56)
            gpt_meta[slug].update({"context_window": fb["context_window"], "max_completion_tokens": fb["max_completion_tokens"], "knowledge_cutoff": fb["knowledge_cutoff"], "reasoning_efforts": fb["reasoning_efforts"], "default_reasoning": fb["default_reasoning"]})

    try:
        ds_pricing = fetch(DEEPSEEK_PRICING_URL)
        ds_chat_api = fetch(DEEPSEEK_CHAT_API_URL)
        ds_meta = parse_deepseek_docs(ds_pricing, ds_chat_api)
        latest_ds_pro, latest_ds_flash = discover_deepseek_stable_models(ds_pricing)
        fetched += [DEEPSEEK_PRICING_URL, DEEPSEEK_CHAT_API_URL]
    except Exception as e:
        if not args.quiet:
            print(f"warn: DeepSeek docs fetch failed: {e}", file=sys.stderr)
        ds_meta = dict(FALLBACK_DEEPSEEK_V4)
        latest_ds_pro, latest_ds_flash = "deepseek-v4-pro", "deepseek-v4-flash"

    old_path = Path(args.catalog)
    # Codex TUI sorts `/model` choices by ascending `priority`; keep latest
    # stable GPT variants at the top, then DeepSeek stable defaults, then
    # compatibility/legacy GPT entries.
    models = []
    for priority, slug in enumerate(latest_variants, start=1):
        meta = gpt_meta[slug]
        models.append(
            gpt_entry(
                slug,
                display_name_from_slug(slug),
                meta["description"],
                priority,
                meta,
            )
        )

    ds_priority = max(10, len(models) + 2)
    models.extend([
        deepseek_entry(
            latest_ds_pro,
            display_name_from_slug(latest_ds_pro),
            f"{display_name_from_slug(latest_ds_pro)} official stable model",
            ds_priority,
            ds_meta,
        ),
        deepseek_entry(
            latest_ds_flash,
            display_name_from_slug(latest_ds_flash),
            f"{display_name_from_slug(latest_ds_flash)} official stable model",
            ds_priority + 1,
            ds_meta,
        ),
    ])
    models.append(
        gpt_entry(
            latest_alias,
            display_name_from_slug(latest_alias),
            f"Alias for {display_name_from_slug(latest_model)}",
            ds_priority + 2,
            gpt_meta[latest_model],
            alias_target=latest_model,
        )
    )

    legacy_order = [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    legacy_slugs = [
        slug
        for slug in legacy_order
        if slug in gpt_meta and slug not in latest_variants
    ]
    for offset, slug in enumerate(legacy_slugs, start=1):
        meta = gpt_meta[slug]
        models.append(
            gpt_entry(
                slug,
                display_name_from_slug(slug),
                meta["description"],
                ds_priority + 10 + offset,
                meta,
            )
        )

    catalog = {
        "models": models,
        "metadata": {
            "last_official_refresh": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
            "latest_openai_model": latest_model,
            "latest_openai_alias": latest_alias,
            "latest_openai_variants": latest_variants,
            "latest_deepseek_pro": latest_ds_pro,
            "latest_deepseek_flash": latest_ds_flash,
            "sources": fetched,
        },
    }
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.stamp).write_text(catalog["metadata"]["last_official_refresh"] + "\n", encoding="utf-8")
    if not args.quiet:
        print(f"wrote {old_path}")
        for m in models:
            print(f"- {m['slug']}: ctx={m['context_window']} max_out={m['max_completion_tokens']} default_reasoning={m['default_reasoning_level']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
