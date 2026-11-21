"""Shared, secret-free configuration for Codex CLI model providers."""
import json
import os
import re


ENV_PROVIDER = 'GAMECRAFT_CODEX_PROVIDER'
ENV_BASE_URL = 'GAMECRAFT_CODEX_BASE_URL'
ENV_API_KEY_ENV = 'GAMECRAFT_CODEX_API_KEY_ENV'
ENV_WEBSOCKETS = 'GAMECRAFT_CODEX_SUPPORTS_WEBSOCKETS'
_STANDARD_SECRETS = ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL')
DEFAULT_PROVIDER = 'openai'


def add_arguments(parser):
    parser.add_argument('--codex-provider', default=os.getenv(ENV_PROVIDER, DEFAULT_PROVIDER))
    parser.add_argument('--codex-base-url', default=os.getenv(ENV_BASE_URL))
    parser.add_argument('--codex-api-key-env', default=os.getenv(ENV_API_KEY_ENV))
    parser.add_argument('--codex-supports-websockets', action='store_true',
                        default=os.getenv(ENV_WEBSOCKETS, '').lower() in ('1', 'true', 'yes'))


def from_args(args):
    return validate({
        'codex_provider': getattr(args, 'codex_provider', DEFAULT_PROVIDER),
        'codex_base_url': getattr(args, 'codex_base_url', None),
        'codex_api_key_env': getattr(args, 'codex_api_key_env', None),
        'codex_supports_websockets': getattr(args, 'codex_supports_websockets', False),
    })


def from_environment():
    return validate({
        'codex_provider': os.getenv(ENV_PROVIDER, DEFAULT_PROVIDER),
        'codex_base_url': os.getenv(ENV_BASE_URL),
        'codex_api_key_env': os.getenv(ENV_API_KEY_ENV),
        'codex_supports_websockets': os.getenv(ENV_WEBSOCKETS, '').lower() in ('1', 'true', 'yes'),
    })


def validate(config):
    provider = config.get('codex_provider', DEFAULT_PROVIDER)
    if not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', provider):
        raise ValueError('Codex provider must contain only letters, digits, underscore, or hyphen')
    result = {'codex_provider': provider}
    if provider == 'openai':
        return result
    base_url = config.get('codex_base_url')
    env_key = config.get('codex_api_key_env')
    if not isinstance(base_url, str) or not base_url.startswith('https://'):
        raise ValueError('Custom Codex provider requires an https --codex-base-url')
    if not isinstance(env_key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env_key):
        raise ValueError('Custom Codex provider requires a valid --codex-api-key-env name')
    result.update(codex_base_url=base_url.rstrip('/'), codex_api_key_env=env_key,
                  codex_supports_websockets=bool(config.get('codex_supports_websockets', False)))
    return result


def cli_args(config):
    cfg = validate(config)
    provider = cfg['codex_provider']
    args = ['-c', f'model_provider={json.dumps(provider)}']
    if provider == 'openai':
        return args
    prefix = f'model_providers.{provider}'
    values = {'name': provider, 'base_url': cfg['codex_base_url'],
              'env_key': cfg['codex_api_key_env'], 'wire_api': 'responses',
              'requires_openai_auth': False,
              'supports_websockets': cfg['codex_supports_websockets']}
    for key, value in values.items():
        args += ['-c', f'{prefix}.{key}={json.dumps(value)}']
    return args


def child_environment(config, source=None):
    cfg = validate(config)
    env = dict(os.environ if source is None else source)
    keep = cfg.get('codex_api_key_env')
    for name in _STANDARD_SECRETS:
        if name != keep:
            env.pop(name, None)
    if keep and not env.get(keep):
        raise RuntimeError(f'Missing Codex provider credential: {keep}')
    return env


def environment_values(config):
    cfg = validate(config)
    values = {ENV_PROVIDER: cfg['codex_provider']}
    if cfg['codex_provider'] != 'openai':
        values.update({ENV_BASE_URL: cfg['codex_base_url'],
                       ENV_API_KEY_ENV: cfg['codex_api_key_env'],
                       ENV_WEBSOCKETS: 'true' if cfg['codex_supports_websockets'] else 'false'})
    return values
