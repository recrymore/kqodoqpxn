import unittest
from unittest.mock import patch
import app

class CopyMirrorTests(unittest.TestCase):
    def test_copy_exact_command(self):
        with patch.object(app,'create_link',return_value='abcdefghijklmnop') as create, patch.object(app,'api',return_value={'username':'TestBot'}),patch.object(app,'send') as send:
            app.send_link(42)
            create.assert_called_once_with(42)
            args=send.call_args.args
            self.assertEqual(args[0],42)
            self.assertIn('/connect@TestBot abcdefghijklmnop',args[1])
            self.assertEqual(args[2]['inline_keyboard'][0][0]['copy_text']['text'],'/connect@TestBot abcdefghijklmnop')
    def test_payload_consistent(self):
        with patch.object(app,'create_link',return_value='abcdefghijklmnop'),patch.object(app,'api',return_value={'username':'TestBot'}):
            p=app.link_payload(42)
            self.assertIn(p['command'],p['instructions'])
            self.assertEqual(p['code'],'abcdefghijklmnop')
    def test_admin_modes(self):
        self.assertEqual(app.configured_admins({}),{8677315356})
        self.assertEqual(app.configured_admins({'TRACKER_MIRROR_MODE':'1','MIRROR_ADMIN_ID':'42'}),{8677315356})
    def test_mirror_no_main_data_api(self):
        with patch.object(app,'MIRROR_MODE',True),patch.object(app,'ADMIN_IDS',{42}),patch.object(app.see_api,'enabled',return_value=True):
            self.assertFalse(app.see_enabled(42))
            with self.assertRaises(ValueError):app.inspect_one(42,'PlushPepe-1')
    def test_mirror_instructions_and_link(self):
        with patch.object(app,'send') as send:
            app.mirror_menu(42)
            args=send.call_args.args
            self.assertIn('/partner @username_bot',args[1]);self.assertIn('Не отправляй токен',args[1]);self.assertEqual(args[2]['inline_keyboard'][0][0]['url'],'https://t.me/BotFather')
    def test_mirror_command_gated(self):
        msg={'chat':{'id':42,'type':'private'},'from':{'id':42},'text':'/mirror'}
        with patch.object(app,'gate',return_value=False),patch.object(app,'mirror_menu') as menu:
            app.handle(msg);menu.assert_not_called()
        with patch.object(app,'gate',return_value=True),patch.object(app,'mirror_menu') as menu:
            app.handle(msg);menu.assert_called_once_with(42)
