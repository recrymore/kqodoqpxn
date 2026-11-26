import tempfile
import unittest
from pathlib import Path
from start import configure


class ReleaseConfigTests(unittest.TestCase):
    def test_persistent_paths_and_host(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'BOT_TOKEN':'test-only','WEBAPP_URL':'https://example.org/','DATA_DIR':directory}
            configure(env)
            self.assertEqual(env['WEBAPP_URL'], 'https://example.org')
            self.assertEqual(env['HOST'], '0.0.0.0')
            self.assertEqual(env['PORT'], '8000')
            self.assertEqual(Path(env['DB_PATH']), Path(directory)/'tracker.sqlite3')
            self.assertEqual(Path(env['CONFIG_PATH']), Path(directory)/'config.json')
            self.assertEqual(Path(env['PARTNER_DB_PATH']), Path(directory)/'partners.sqlite3')

    def test_missing_token(self):
        with self.assertRaisesRegex(ValueError,'BOT_TOKEN'):
            configure({'WEBAPP_URL':'https://example.org'})

    def test_invalid_origin(self):
        for url in ('http://example.org','https://example.org/admin','https://user:pass@example.org','https://example.org/?q=1','https://example.org:bad','https://'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError,'WEBAPP_URL'):
                configure({'BOT_TOKEN':'test-only','WEBAPP_URL':url})

    def test_invalid_port(self):
        for port in ('0','65536','abc'):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError,'PORT'):
                configure({'BOT_TOKEN':'test-only','WEBAPP_URL':'https://example.org','PORT':port})

    def test_existing_paths_and_port_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.sqlite3'
            env={'BOT_TOKEN':'test-only','WEBAPP_URL':'https://example.org','DATA_DIR':directory,'DB_PATH':str(path),'HOST':'127.0.0.1','PORT':'9000'}
            configure(env)
            self.assertEqual(env['DB_PATH'],str(path))
            self.assertEqual(env['PORT'],'9000')
            self.assertEqual(env['HOST'],'127.0.0.1')
