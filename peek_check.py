"""One-off anonymous market interface availability check; no data extraction."""
import os
from playwright.sync_api import sync_playwright, Error

URL = 'https://app.peek.tg/search'
MARKERS = ('Market Filters', 'All gifts', 'All collections', 'All backgrounds', 'All patterns')

def market_status(page):
    # Check only known public market interface labels; never export DOM or results.
    return {label: page.get_by_text(label, exact=True).first.is_visible() for label in MARKERS}

def main():
    with sync_playwright() as p:
        options = {'headless': True}
        if os.getenv('CHROMIUM_PATH'):
            options['executable_path'] = os.environ['CHROMIUM_PATH']
        with p.chromium.launch(**options) as browser:
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            try:
                response = page.goto(URL, wait_until='domcontentloaded', timeout=25000)
                if response is None:
                    print('UNAVAILABLE: no HTTP response'); return 2
                print(f'HTTP status: {response.status}')
                if response.status >= 400:
                    print('STOP: site refused or could not serve request'); return 2
                if not page.url.startswith('https://app.peek.tg/'):
                    print('STOP: unexpected redirect'); return 2
                try:
                    page.get_by_text('Market Filters', exact=True).first.wait_for(state='visible', timeout=10000)
                except Error:
                    pass
                flags = market_status(page)
                for label, visible in flags.items():
                    print(f'{label}: {"visible" if visible else "not visible"}')
                if not flags['Market Filters']:
                    print('INCONCLUSIVE: market interface not confirmed; no further requests'); return 3
                print('OK: public market interface visible. No listing or contact data collected.'); return 0
            except Error:
                print('UNAVAILABLE: browser navigation failed'); return 2
            finally:
                context.close()

if __name__ == '__main__':
    raise SystemExit(main())
