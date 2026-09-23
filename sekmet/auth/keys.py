"""Key generation and JWK helpers for SMART Backend Services."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm


def generate_private_key(alg: str = "RS384"):
    if alg.startswith("ES"):
        return ec.generate_private_key(ec.SECP384R1() if alg == "ES384" else ec.SECP256R1())
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def save_private_key(key, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    p.chmod(0o600)


def load_private_key(path: str):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def load_public_key(path: str):
    data = Path(path).read_bytes()
    try:
        return serialization.load_pem_public_key(data)
    except ValueError:
        return serialization.load_pem_private_key(data, password=None).public_key()


def _b64(n: bytes) -> str:
    return base64.urlsafe_b64encode(n).rstrip(b"=").decode()


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint."""
    if jwk["kty"] == "RSA":
        members = {"e": jwk["e"], "kty": "RSA", "n": jwk["n"]}
    else:
        members = {"crv": jwk["crv"], "kty": "EC", "x": jwk["x"], "y": jwk["y"]}
    return _b64(hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode()).digest())


def public_jwk(private_or_public_key, alg: str | None = None, kid: str | None = None) -> dict:
    key = private_or_public_key
    pub = key.public_key() if hasattr(key, "public_key") and not isinstance(
        key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey)) else key
    if isinstance(pub, rsa.RSAPublicKey):
        jwk = json.loads(RSAAlgorithm.to_jwk(pub))
        alg = alg or "RS384"
    else:
        jwk = json.loads(ECAlgorithm.to_jwk(pub))
        alg = alg or ("ES384" if jwk.get("crv") == "P-384" else "ES256")
    jwk.update({"alg": alg, "use": "sig", "key_ops": ["verify"]})
    jwk["kid"] = kid or thumbprint(jwk)
    return jwk


def key_alg(key) -> str:
    if isinstance(key, rsa.RSAPrivateKey):
        return "RS384"
    return "ES384" if key.curve.name == "secp384r1" else "ES256"


def jwk_to_key(jwk: dict):
    if jwk.get("kty") == "RSA":
        return RSAAlgorithm.from_jwk(json.dumps(jwk))
    if jwk.get("kty") == "EC":
        return ECAlgorithm.from_jwk(json.dumps(jwk))
    raise ValueError(f"Unsupported JWK kty {jwk.get('kty')}")
