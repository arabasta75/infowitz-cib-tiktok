"""
llm.py — Appels LLM pour HLB Engine (Groq, OpenAI, Mistral, Gemini)
"""
import json
import os
import requests


def analyze_with_openai(prompt: str, context: str, model: str, api_key: str) -> dict:
    r = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={
            'model': model,
            'messages': [
                {'role': 'system', 'content': 'Tu es un expert en détection de comportements inauthentiques sur les réseaux sociaux. Réponds en français, de manière structurée.'},
                {'role': 'user', 'content': f"{prompt}\n\n{context}"},
            ],
            'temperature': 0.3,
            'max_tokens': 4000,
        },
        timeout=90,
    )
    data = r.json()
    if 'error' in data:
        raise ValueError(data['error'].get('message', str(data['error'])))
    content = data['choices'][0]['message']['content']
    tokens  = data.get('usage', {}).get('total_tokens', 0)
    pricing = {'gpt-4o-mini': 0.000600/1000, 'gpt-4o': 0.002500/1000, 'gpt-4.1-mini': 0.000400/1000, 'gpt-4.1': 0.002000/1000}
    return {'result': content, 'tokens_used': tokens, 'cost_usd': tokens * pricing.get(model, 0.005/1000)}


def analyze_with_groq(prompt: str, context: str, model: str, api_key: str) -> dict:
    """Appel Groq avec retry sur 429 + timeout + connection error + 5xx."""
    import time as _time
    full_prompt = f"{prompt}\n\n{context}" if context else prompt
    MAX_PROMPT_CHARS = 60_000
    if len(full_prompt) > MAX_PROMPT_CHARS:
        full_prompt = full_prompt[:MAX_PROMPT_CHARS] + "\n\n[…contenu tronqué pour quota Groq…]"
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': 'Tu es un expert en détection de comportements inauthentiques sur les réseaux sociaux. Réponds en français, de manière structurée.'},
            {'role': 'user', 'content': full_prompt},
        ],
        'temperature': 0.3,
        'max_tokens': 4000,
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json=payload,
                timeout=120,
            )
        except requests.exceptions.Timeout:
            last_err = f'Groq timeout 120s (attempt {attempt+1}/3)'
            _time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError as ce:
            last_err = f'Groq connection: {str(ce)[:120]}'
            _time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            retry_after = float(r.headers.get('Retry-After') or 2 ** attempt)
            _time.sleep(min(retry_after, 8))
            last_err = f'Groq 429 rate limit (attempt {attempt+1}/3)'
            continue
        if r.status_code == 413:
            raise ValueError(f'Groq 413 : prompt trop long ({len(full_prompt)} chars)')
        if r.status_code >= 500:
            last_err = f'Groq HTTP {r.status_code} (attempt {attempt+1}/3): {r.text[:200]}'
            _time.sleep(2 ** attempt)
            continue
        try:
            data = r.json()
        except Exception as je:
            raise ValueError(f'Groq JSON parse fail: {je} | raw: {r.text[:300]}')
        if 'error' in data:
            err_msg = data['error'].get('message', str(data['error']))
            if 'rate' in err_msg.lower() or '429' in err_msg or 'context_length' in err_msg.lower():
                _time.sleep(2 ** attempt)
                last_err = err_msg
                continue
            raise ValueError(f'Groq API error: {err_msg}')
        if 'choices' not in data or not data['choices']:
            raise ValueError(f'Groq response invalide : {str(data)[:300]}')
        content = data['choices'][0]['message']['content']
        tokens  = data.get('usage', {}).get('total_tokens', 0)
        return {'result': content, 'tokens_used': tokens, 'cost_usd': 0.0}
    raise ValueError(f'Groq échec après 3 tentatives: {last_err}')


def analyze_with_mistral(prompt: str, context: str, model: str, api_key: str) -> dict:
    r = requests.post(
        'https://api.mistral.ai/v1/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={
            'model': model,
            'messages': [
                {'role': 'system', 'content': 'Tu es un expert en détection de comportements inauthentiques sur les réseaux sociaux. Réponds en français, de manière structurée.'},
                {'role': 'user', 'content': f"{prompt}\n\n{context}"},
            ],
            'temperature': 0.3,
            'max_tokens': 4000,
        },
        timeout=90,
    )
    data = r.json()
    if 'error' in data:
        err = data['error']
        raise ValueError(err.get('message', str(err)) if isinstance(err, dict) else str(err))
    content = data['choices'][0]['message']['content']
    tokens  = data.get('usage', {}).get('total_tokens', 0)
    return {'result': content, 'tokens_used': tokens, 'cost_usd': 0.0}


def analyze_with_gemini(prompt: str, context: str, model: str, api_key: str) -> dict:
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    r = requests.post(
        url, params={'key': api_key},
        json={
            'contents': [{'parts': [{'text': f"Tu es un expert en détection de comportements inauthentiques.\n\n{prompt}\n\n{context}"}]}],
            'generationConfig': {'temperature': 0.3, 'maxOutputTokens': 4000},
        },
        timeout=90,
    )
    data = r.json()
    if 'error' in data:
        raise ValueError(data['error'].get('message', str(data['error'])))
    content = data['candidates'][0]['content']['parts'][0]['text']
    tokens  = data.get('usageMetadata', {}).get('totalTokenCount', 0)
    return {'result': content, 'tokens_used': tokens, 'cost_usd': 0.0}


def call_llm(prompt: str, context: str, model: str, user_cfg: dict) -> dict:
    """Appel LLM générique avec sélection automatique du provider."""
    provider = model.split(':')[0] if ':' in model else 'groq'
    model_name = model.split(':', 1)[1] if ':' in model else model
    key_map = {
        'groq': 'groq_key', 'openai': 'openai_key',
        'mistral': 'mistral_key', 'gemini': 'gemini_key',
    }
    api_key = str(user_cfg.get(key_map.get(provider, 'groq_key')) or '').strip()

    if not api_key:
        for p, k in key_map.items():
            v = str(user_cfg.get(k) or '').strip()
            if v:
                api_key = v
                provider = p
                model_name = {
                    'groq': 'llama-3.3-70b-versatile',
                    'openai': 'gpt-4o-mini',
                    'mistral': 'mistral-small-latest',
                    'gemini': 'gemini-1.5-flash',
                }.get(p, model_name)
                break

    if not api_key:
        raise ValueError("Aucune clé LLM configurée")

    fn = {
        'groq': analyze_with_groq,
        'openai': analyze_with_openai,
        'mistral': analyze_with_mistral,
        'gemini': analyze_with_gemini,
    }.get(provider)
    if not fn:
        raise ValueError(f"Provider {provider} non supporté")

    result = fn(prompt, context, model_name, api_key)
    result['model_used'] = f'{provider}:{model_name}'
    return result
