from os import urandom
from io import StringIO
from flask import Flask, abort, request, Response, g
from .ca import CertificateAuthority, ca_from_storage, get_ca
from base64 import b64decode, b64encode
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from asn1crypto.csr import CertificationRequestInfo
from asn1crypto.cms import ContentInfo, ContentType
from .message import SCEPMessage
from .enums import MessageType, PKIStatus, FailInfo
from .builders import PKIMessageBuilder, create_degenerate_certificate

FORCE_DEGENERATE_FOR_SINGLE_CERT = False
CACAPS = ('POSTPKIOperation', 'SHA-256', 'AES')


class WSGIChunkedBodyCopy(object):
    '''WSGI wrapper that handles chunked encoding of the request body. Copies
    de-chunked body to a WSGI environment variable called `body_copy` (so best
    not to use with large requests lest memory issues crop up.'''
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        wsgi_input = environ.get('wsgi.input')
        if 'chunked' in environ.get('HTTP_TRANSFER_ENCODING', '') and \
                        environ.get('CONTENT_LENGTH', '') == '' and \
                wsgi_input:

            body = b''
            sz = int(wsgi_input.readline(), 16)
            while sz > 0:
                body += wsgi_input.read(sz + 2)[:-2]
                sz = int(wsgi_input.readline(), 16)

            environ['body_copy'] = body
            environ['wsgi.input'] = body

        return self.app(environ, start_response)


app = Flask(__name__)
app.config.from_object('scep.default_settings')
app.config.from_envvar('SCEP_SETTINGS')
app.wsgi_app = WSGIChunkedBodyCopy(app.wsgi_app)

with app.app_context():
    g.ca = ca_from_storage(app.config['CA_ROOT'])

@app.route('/cgi-bin/pkiclient.exe', methods=['GET', 'POST'])
@app.route('/scep', methods=['GET', 'POST'])
@app.route('/', methods=['GET', 'POST'])
def scep():
    op = request.args.get('operation')
    mdm_ca = ca_from_storage(app.config['CA_ROOT'])

    if op == 'GetCACert':
        certs = [mdm_ca.certificate]

        if len(certs) == 1 and not FORCE_DEGENERATE_FOR_SINGLE_CERT:
            return Response(certs[0].public_bytes(Encoding.DER), mimetype='application/x-x509-ca-cert')
        elif len(certs):
            raise ValueError('cryptography cannot produce degenerate pkcs7 certs')
            # p7_degenerate = degenerate_pkcs7_der(certs)
            # return Response(p7_degenerate, mimetype='application/x-x509-ca-ra-cert')
    elif op == 'GetCACaps':
        return '\n'.join(CACAPS)
    elif op == 'PKIOperation':
        if request.method == 'GET':
            msg = request.args.get('message')
            # note: OS X improperly encodes the base64 query param by not
            # encoding spaces as %2B and instead leaving them as +'s
            msg = b64decode(msg.replace(' ', '+'))
        elif request.method == 'POST':
            # workaround for Flask/Werkzeug lack of chunked handling
            if 'chunked' in request.headers.get('Transfer-Encoding', ''):
                msg = request.environ['body_copy']
            else:
                msg = request.data

        req = SCEPMessage.parse(msg)
        app.logger.debug('Received SCEPMessage, details follow')
        print("{:<20}: {}".format('Transaction ID', req.transaction_id))
        print("{:<20}: {}".format('Message Type', req.message_type))
        print("{:<20}: {}".format('PKI Status', req.pki_status))
        if req.sender_nonce is not None:
            print("{:<20}: {}".format('Sender Nonce', b64encode(req.sender_nonce)))
        if req.recipient_nonce is not None:
            print("{:<20}: {}".format('Recipient Nonce', b64encode(req.recipient_nonce)))
        
        x509name, serial = req.signer
        print("{:<20}: {}".format('Issuer X.509 Name', x509name))
        print("{:<20}: {}".format('Issuer S/N', serial))

        if req.message_type == MessageType.PKCSReq:
            app.logger.debug('received PKCSReq SCEP message')

            cakey = mdm_ca.private_key
            cacert = mdm_ca.certificate

            der_req = req.get_decrypted_envelope_data(
                cacert,
                cakey,
            )

            cert_req = x509.load_der_x509_csr(der_req, backend=default_backend())
            req_info_bytes = cert_req.tbs_certrequest_bytes

            # Check the challenge password
            req_info = CertificationRequestInfo.load(req_info_bytes)
            for attr in req_info['attributes']:
                if attr['type'].native == 'challenge_password':
                    assert len(attr['values']) == 1
                    challenge_password = attr['values'][0].native
                    print("{:<20}: {}".format('Challenge Password', challenge_password))
                    break  # TODO: if challenge password fails send pkcs#7 with pki status failure

            # CA should persist all signed certs itself
            new_cert = mdm_ca.sign(cert_req)

            reply = PKIMessageBuilder(cacert, cakey).message_type(
                MessageType.CertRep
            ).transaction_id(
                req.transaction_id
            ).pki_status(
                PKIStatus.SUCCESS
            ).recipient_nonce(
                req.sender_nonce
            # ).sender_nonce(
            #     urandom(16)
            ).add_recipient(
                cert_req
            ).issued(
                new_cert
            ).encrypt(
                create_degenerate_certificate(new_cert).dump()
            ).certificates(
                cacert
            ).finalize()

            res = SCEPMessage.parse(reply.dump())
            app.logger.debug('Reply with CertRep, details follow')
            print("{:<20}: {}".format('Transaction ID', res.transaction_id))
            print("{:<20}: {}".format('Message Type', res.message_type))
            print("{:<20}: {}".format('PKI Status', res.pki_status))
            if res.sender_nonce is not None:
                print("{:<20}: {}".format('Sender Nonce', b64encode(res.sender_nonce)))
            if res.recipient_nonce is not None:
                print("{:<20}: {}".format('Recipient Nonce', b64encode(res.recipient_nonce)))

            x509name, serial = res.signer
            print("{:<20}: {}".format('Issuer X.509 Name', x509name))
            print("{:<20}: {}".format('Issuer S/N', serial))

                        

            with open('/tmp/reply.bin', 'wb') as fd:
                fd.write(reply.dump())

            return Response(reply.dump(), mimetype='application/x-pki-message')
        else:
            app.logger.error('unhandled SCEP message type: %d', req.message_type)
            return ''
    else:
        abort(404, 'unknown SCEP operation')

