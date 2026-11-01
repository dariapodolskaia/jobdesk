import io, json, unittest, urllib.error, urllib.parse
from unittest.mock import patch
from scripts.connect_hh import request_token


class HHSetupTests(unittest.TestCase):
    def test_credentials_sent_only_in_post_body(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'access_token':'test-token'}).encode())) as send:
            self.assertEqual(request_token('test-id','test-secret','qa@example.com'),'test-token')
        request=send.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.hh.ru/token');self.assertEqual(request.method,'POST')
        self.assertNotIn('test-secret',request.full_url);self.assertNotIn('test-secret',str(request.headers))
        self.assertEqual(urllib.parse.parse_qs(request.data.decode())['grant_type'],['client_credentials'])

    def test_server_error_does_not_echo_response_with_credentials(self):
        error=urllib.error.HTTPError('https://api.hh.ru/token',403,'Forbidden',{},io.BytesIO(b'test-secret'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(ValueError) as caught:request_token('test-id','test-secret','qa@example.com')
        self.assertIn('403',str(caught.exception));self.assertNotIn('test-secret',str(caught.exception))

    def test_missing_token_not_accepted(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{}')):
            with self.assertRaisesRegex(ValueError,'access_token'):request_token('test-id','test-secret','qa@example.com')
