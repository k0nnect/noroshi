"""A test store written by the real crawler: local HTTPS sites, each its own server under its own name.

The names are invented; the tests' strict resolver maps them to 127.0.0.1, so no request can leave the machine. One
self-signed certificate names them all and is trusted through SSL_CERT_FILE, so the fetcher runs unchanged.
"""
import datetime
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pytest_httpserver import HTTPServer


def make_certificate(folder: Path, names: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate for these host names: (certificate file, key file)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "noroshi test sites")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    folder.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


class Sites:
    """Local HTTPS servers, one per site name. `phase` lets a handler answer differently in each pass."""

    def __init__(self, folder: Path, names: list[str], local_dns: set[str]):
        self.cert, key = make_certificate(folder, names)
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.load_cert_chain(self.cert, key)
        local_dns.update(names)
        self.servers: dict[str, HTTPServer] = {}
        self.phase = 1

    def add(self, name: str) -> HTTPServer:
        s = HTTPServer(host="127.0.0.1", port=0, ssl_context=self.context)
        s.no_handler_status_code = 404
        s.start()
        s.host = name  # url_for() names this site
        self.servers[name] = s
        return s

    def url(self, name: str, path: str = "/") -> str:
        return self.servers[name].url_for(path)

    def log(self, name: str) -> list[str]:
        return [req.path for req, _ in self.servers[name].log]

    def stop(self) -> None:
        for s in self.servers.values():
            if s.is_running():
                s.stop()
