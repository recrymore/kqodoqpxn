"""Release entry point. Configure environment before importing the application."""
import os
from pathlib import Path
from urllib.parse import urlsplit


def configure(env):
    if not env.get('BOT_TOKEN', '').strip():
        raise ValueError('BOT_TOKEN is required')
    url = env.get('WEBAPP_URL', '').strip().rstrip('/')
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == 'https' and bool(parts.hostname)
                 and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and not parts.path)
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('WEBAPP_URL must be the public HTTPS origin, without a path')
    try:
        port = int(env.get('PORT', '8000'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('PORT must be an integer between 1 and 65535') from None
    data = Path(env.get('DATA_DIR', str(Path(__file__).resolve().parent / 'data'))).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    env['WEBAPP_URL'] = url
    env.setdefault('HOST', '0.0.0.0')
    env['PORT'] = str(port)
    env.setdefault('DB_PATH', str(data / 'tracker.sqlite3'))
    env.setdefault('CONFIG_PATH', str(data / 'config.json'))
    env.setdefault('PARTNER_DB_PATH', str(data / 'partners.sqlite3'))
    # Ensure directories exist and are writable before starting Telegram polling.
    for key in ('DB_PATH', 'CONFIG_PATH', 'PARTNER_DB_PATH'):
        path = Path(env[key]).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() or not os.access(path.parent, os.W_OK):
            raise ValueError(f'{key} must point to a writable file location')
        env[key] = str(path)


def main():
    try:
        configure(os.environ)
    except (ValueError, OSError) as exc:
        raise SystemExit('Startup configuration invalid: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)) from None
    import app
    app.main()


if __name__ == '__main__':
    main()
