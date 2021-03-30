from io import BytesIO
import json

from buidl.ecc import G, N, PrivateKey, S256Point
from buidl.helper import (
    big_endian_to_int,
    byte_to_int,
    encode_base58_checksum,
    hmac_sha512,
    hmac_sha512_kdf,
    int_to_big_endian,
    int_to_byte,
    raw_decode_base58,
    sha256,
)

from buidl.mnemonic import secure_mnemonic, get_word_lookup, get_word_list


MAINNET_XPRV = bytes.fromhex("0488ade4")
MAINNET_XPUB = bytes.fromhex("0488b21e")
MAINNET_YPRV = bytes.fromhex("049d7878")
MAINNET_YPUB = bytes.fromhex("049d7cb2")
MAINNET_ZPRV = bytes.fromhex("04b2430c")
MAINNET_ZPUB = bytes.fromhex("04b24746")
TESTNET_XPRV = bytes.fromhex("04358394")
TESTNET_XPUB = bytes.fromhex("043587cf")
TESTNET_YPRV = bytes.fromhex("044a4e28")
TESTNET_YPUB = bytes.fromhex("044a5262")
TESTNET_ZPRV = bytes.fromhex("045f18bc")
TESTNET_ZPUB = bytes.fromhex("045f1cf6")


class AddressHelper(object):
    @classmethod
    def get(cls, xprv, index, internal=0):
        """Fetch the bech32 address at given index for given xprv.

        If internal is specified and set to 1, external / change address is returned.
        """

        return xprv.traverse('m/84h/0h/0h/{}/{}'.format(internal, index)).bech32_address()

    @classmethod
    def get_desc(cls, xprv, num):
        """Fetch a descriptor for the first num bech32 addresses at standard path for given xprv.
        
        Both internal and external addresses for m/84'/0'/0'/<internal>/<num> are returned.
        """

        addrs = []
        for internal in (0, 1):
            for index in range(num):
                addrs.append('addr({})'.format(cls.get(xprv, index, internal=internal)))
        return json.dumps(addrs)


class HDPrivateKey(object):
    def __init__(
        self,
        private_key,
        chain_code,
        depth=0,
        parent_fingerprint=b"\x00\x00\x00\x00",
        child_number=0,
        testnet=False,
    ):
        # the main secret, should be a PrivateKey object
        self.private_key = private_key
        self.private_key.testnet = testnet
        # the code to make derivation deterministic
        self.chain_code = chain_code
        # level the current key is at in the heirarchy
        self.depth = depth
        # fingerprint of the parent key
        self.parent_fingerprint = parent_fingerprint
        # what order child this is
        self.child_number = child_number
        self.testnet = testnet
        # keep a copy of the corresponding public key
        self.pub = HDPublicKey(
            point=private_key.point,
            chain_code=chain_code,
            depth=depth,
            parent_fingerprint=parent_fingerprint,
            child_number=child_number,
            testnet=testnet,
        )

    def wif(self):
        return self.private_key.wif()

    def sec(self):
        return self.pub.sec()

    def hash160(self):
        return self.pub.hash160()

    def p2pkh_script(self):
        return self.pub.p2pkh_script()

    def p2wpkh_script(self):
        return self.pub.p2wpkh_script()

    def p2sh_p2wpkh_script(self):
        return self.pub.p2sh_p2wpkh_script()

    def address(self):
        return self.pub.address()

    def bech32_address(self):
        return self.pub.bech32_address()

    def p2sh_p2wpkh_address(self):
        return self.pub.p2sh_p2wpkh_address()

    def __repr__(self):
        return self.xprv()

    @classmethod
    def from_seed(cls, seed, testnet=False):
        # get hmac_sha512 with b'Bitcoin seed' and seed
        h = hmac_sha512(b"Bitcoin seed", seed)
        # create the private key using the first 32 bytes in big endian
        private_key = PrivateKey(secret=big_endian_to_int(h[:32]))
        # chaincode is the last 32 bytes
        chain_code = h[32:]
        # return an instance of the class
        return cls(
            private_key=private_key,
            chain_code=chain_code,
            testnet=testnet,
        )

    def child(self, index):
        """Returns the child HDPrivateKey at a particular index.
        Hardened children return for indices >= 0x8000000.
        """
        # if index >= 0x80000000
        if index >= 0x80000000:
            # the message data is the private key secret in 33 bytes in
            #  big-endian and the index in 4 bytes big-endian.
            data = int_to_big_endian(self.private_key.secret, 33) + int_to_big_endian(
                index, 4
            )
        else:
            # the message data is the public key compressed SEC
            #  and the index in 4 bytes big-endian.
            data = self.private_key.point.sec() + int_to_big_endian(index, 4)
        # get the hmac_sha512 with chain code and data
        h = hmac_sha512(self.chain_code, data)
        # the new secret is the first 32 bytes as a big-endian integer
        #  plus the secret mod N
        secret = (big_endian_to_int(h[:32]) + self.private_key.secret) % N
        # create the PrivateKey object
        private_key = PrivateKey(secret=secret)
        # the chain code is the last 32 bytes
        chain_code = h[32:]
        # depth is whatever the current depth + 1
        depth = self.depth + 1
        # parent_fingerprint is the fingerprint of this node
        parent_fingerprint = self.fingerprint()
        # child number is the index
        child_number = index
        # return a new HDPrivateKey instance
        return HDPrivateKey(
            private_key=private_key,
            chain_code=chain_code,
            depth=depth,
            parent_fingerprint=parent_fingerprint,
            child_number=child_number,
            testnet=self.testnet,
        )

    def traverse(self, path):
        """Returns the HDPrivateKey at the path indicated.
        Path should be in the form of m/x/y/z where x' means
        hardened"""

        # keep track of the current node starting with self
        current = self
        # split up the path by the '/' splitter, ignore the first
        components = path.split("/")[1:]
        # iterate through the path components
        for child in components:
            # if the child ends with a single quote or h, we have a hardened child
            if child.endswith("'") or child.endswith('h'):
                # index is the integer representation + 0x80000000
                index = int(child[:-1]) + 0x80000000
            # else the index is the integer representation
            else:
                index = int(child)
            # grab the child at the index calculated
            current = current.child(index)
        # return the current child
        return current

    def raw_serialize(self, version):
        # version + depth + parent_fingerprint + child number + chain code + private key
        # start with version, which should be a constant depending on testnet
        raw = version
        # add depth, which is 1 byte using int_to_byte
        raw += int_to_byte(self.depth)
        # add the parent_fingerprint
        raw += self.parent_fingerprint
        # add the child number 4 bytes using int_to_big_endian
        raw += int_to_big_endian(self.child_number, 4)
        # add the chain code
        raw += self.chain_code
        # add the 0 byte and the private key's secret in big endian, 33 bytes
        raw += int_to_big_endian(self.private_key.secret, 33)
        return raw

    def _prv(self, version):
        """Returns the base58-encoded x/y/z prv.
        Expects a 4-byte version."""

        raw = self.raw_serialize(version)
        # return the whole thing base58-encoded
        return encode_base58_checksum(raw)

    def xprv(self):
        # from BIP0032:
        if self.testnet:
            version = TESTNET_XPRV
        else:
            version = MAINNET_XPRV
        return self._prv(version)

    def yprv(self):
        # from BIP0049:
        if self.testnet:
            version = TESTNET_YPRV
        else:
            version = MAINNET_YPRV
        return self._prv(version)

    def zprv(self):
        # from BIP0084:
        if self.testnet:
            version = TESTNET_ZPRV
        else:
            version = MAINNET_ZPRV
        return self._prv(version)

    # passthrough methods
    def fingerprint(self):
        return self.pub.fingerprint()

    def xpub(self, version=None):
        return self.pub.xpub(version=version)

    def ypub(self):
        return self.pub.ypub()

    def zpub(self):
        return self.pub.zpub()

    @classmethod
    def parse(cls, s):
        """Returns a HDPrivateKey from an extended key string"""

        # get the bytes from the base58 using raw_decode_base58
        raw = raw_decode_base58(s)
        # check that the length of the raw is 78 bytes, otherwise raise ValueError
        if len(raw) != 78:
            raise ValueError("Not a proper extended key")
        # create a stream
        stream = BytesIO(raw)
        # return the raw parsing of the stream
        return cls.raw_parse(stream)

    @classmethod
    def raw_parse(cls, s):
        """Returns a HDPrivateKey from a stream"""

        # first 4 bytes are the version
        version = s.read(4)
        # check that the version is one of the TESTNET or MAINNET
        #  private keys, if not raise a ValueError
        if version in (TESTNET_XPRV, TESTNET_YPRV, TESTNET_ZPRV):
            testnet = True
        elif version in (MAINNET_XPRV, MAINNET_YPRV, MAINNET_ZPRV):
            testnet = False
        else:
            raise ValueError("not an xprv, yprv or zprv: {}".format(version))
        # the next byte is depth
        depth = byte_to_int(s.read(1))
        # next 4 bytes are the parent_fingerprint
        parent_fingerprint = s.read(4)
        # next 4 bytes is the child number in big-endian
        child_number = big_endian_to_int(s.read(4))
        # next 32 bytes are the chain code
        chain_code = s.read(32)
        # the next byte should be b'\x00'
        if byte_to_int(s.read(1)) != 0:
            raise ValueError("private key should be preceded by a zero byte")
        # last 32 bytes should be the private key in big endian
        private_key = PrivateKey(secret=big_endian_to_int(s.read(32)))
        # return an instance of the class
        return cls(
            private_key=private_key,
            chain_code=chain_code,
            depth=depth,
            parent_fingerprint=parent_fingerprint,
            child_number=child_number,
            testnet=testnet,
        )

    def _get_address(self, purpose, account=0, external=True, address=0):
        """Returns the proper address among purposes 44', 49' and 84'.
        p2pkh for 44', p2sh-p2wpkh for 49' and p2wpkh for 84'."""
        # if purpose is not one of 44', 49' or 84', raise ValueError
        if purpose not in ("44'", "49'", "84'"):
            raise ValueError(
                "Cannot create an address without a proper purpose: {}".format(purpose)
            )
        # if testnet, coin is 1', otherwise 0'
        if self.testnet:
            coin = "1'"
        else:
            coin = "0'"
        # if external, chain is 0, otherwise 1
        if external:
            chain = "0"
        else:
            chain = "1"
        # create the path m/purpose'/coin'/account'/chain/address
        path = "m/{}/{}/{}'/{}/{}".format(purpose, coin, account, chain, address)
        # get the HDPrivateKey at that location
        hd_priv = self.traverse(path)
        # if 44', return the address
        if purpose == "44'":
            return hd_priv.address()
        # if 49', return the p2sh_p2wpkh_address
        elif purpose == "49'":
            return hd_priv.p2sh_p2wpkh_address()
        # if 84', return the bech32_address
        elif purpose == "84'":
            return hd_priv.bech32_address()

    def get_p2pkh_receiving_address(self, account=0, address=0):
        return self._get_address("44'", account, True, address)

    def get_p2pkh_change_address(self, account=0, address=0):
        return self._get_address("44'", account, False, address)

    def get_p2sh_p2wpkh_receiving_address(self, account=0, address=0):
        return self._get_address("49'", account, True, address)

    def get_p2sh_p2wpkh_change_address(self, account=0, address=0):
        return self._get_address("49'", account, False, address)

    def get_p2wpkh_receiving_address(self, account=0, address=0):
        return self._get_address("84'", account, True, address)

    def get_p2wpkh_change_address(self, account=0, address=0):
        return self._get_address("84'", account, False, address)

    @classmethod
    def generate(cls, password=b"", entropy=0, testnet=False):
        mnemonic = secure_mnemonic(entropy=entropy)
        return mnemonic, cls.from_mnemonic(mnemonic, password=password, testnet=testnet)

    @classmethod
    def from_mnemonic(cls, mnem, password=b"", path="m", testnet=False):
        """Returns a HDPrivateKey object from the mnemonic."""

        # split the mnemonic into words with .split()
        words = mnem.split()
        # check that there are 12, 15, 18, 21 or 24 words
        # if not, raise a ValueError
        if len(words) not in (12, 15, 18, 21, 24):
            raise ValueError("you need 12, 15, 18, 21, or 24 words")
        # calculate the number
        number = 0

        word_list = get_word_list()
        word_lookup = get_word_lookup(word_list)
        # each word is 11 bits
        for word in words:
            # get the index for each word
            index = word_lookup[word]
            # left-shift the index by 11 bits and bitwise-or
            number = (number << 11) | index

        # checksum is the last n bits where n = (# of words / 3)
        checksum_bits_length = len(words) // 3
        # grab the checksum bits
        checksum = number & ((1 << checksum_bits_length) - 1)
        # get the actual number by right-shifting by the checksum bits length
        data_num = number >> checksum_bits_length
        # convert the number to big-endian
        data = int_to_big_endian(data_num, checksum_bits_length * 4)
        # the one byte we get is from sha256 of the data, shifted by
        #  8 - the number of bits we need for the checksum
        computed_checksum = sha256(data)[0] >> (8 - checksum_bits_length)
        # check that the checksum is correct or raise ValueError
        if checksum != computed_checksum:
            raise ValueError("words fail checksum: {}".format(words))
        # normalize in case we got a mnemonic that's just the first 4 letters
        normalized_words = []
        for word in words:
            normalized_words.append(word_list[word_lookup[word]])
        normalized_mnemonic = " ".join(normalized_words)
        # salt is literal bytes 'mnemonic' followed by password
        salt = b"mnemonic" + password
        # the seed is the hmac_sha512_kdf with normalized mnemonic and salt
        seed = hmac_sha512_kdf(normalized_mnemonic, salt)
        # return the HDPrivateKey at the path specified
        return cls.from_seed(seed, testnet=testnet).traverse(path)


class HDPublicKey:
    def __init__(
        self, point, chain_code, depth, parent_fingerprint, child_number, testnet=False
    ):
        self.point = point
        self.chain_code = chain_code
        self.depth = depth
        self.parent_fingerprint = parent_fingerprint
        self.child_number = child_number
        self.testnet = testnet
        self._raw = None

    def __repr__(self):
        return self.xpub()

    def sec(self):
        return self.point.sec()

    def hash160(self):
        return self.point.hash160()

    def p2pkh_script(self):
        return self.point.p2pkh_script()

    def p2wpkh_script(self):
        return self.point.p2wpkh_script()

    def p2sh_p2wpkh_script(self):
        return self.point.p2sh_p2wpkh_script()

    def address(self):
        return self.point.address(testnet=self.testnet)

    def bech32_address(self):
        return self.point.bech32_address(testnet=self.testnet)

    def p2sh_p2wpkh_address(self):
        return self.point.p2sh_p2wpkh_address(testnet=self.testnet)

    def fingerprint(self):
        """Fingerprint is the hash160's first 4 bytes"""
        return self.hash160()[:4]

    def child(self, index):
        """Returns the child HDPrivateKey at a particular index.
        Raises ValueError for indices >= 0x8000000.
        """
        # if index >= 0x80000000, raise a ValueError
        if index >= 0x80000000:
            raise ValueError("child number should always be less than 2^31")
        # data is the SEC compressed and the index in 4 bytes big-endian
        data = self.point.sec() + int_to_big_endian(index, 4)
        # get hmac_sha512 with chain code, data
        h = hmac_sha512(self.chain_code, data)
        # the new public point is the current point +
        #  the first 32 bytes in big endian * G
        point = self.point + big_endian_to_int(h[:32])
        # chain code is the last 32 bytes
        chain_code = h[32:]
        # depth is current depth + 1
        depth = self.depth + 1
        # parent_fingerprint is the fingerprint of this node
        parent_fingerprint = self.fingerprint()
        # child number is the index
        child_number = index
        # return the HDPublicKey instance
        return HDPublicKey(
            point=point,
            chain_code=chain_code,
            depth=depth,
            parent_fingerprint=parent_fingerprint,
            child_number=child_number,
            testnet=self.testnet,
        )

    def traverse(self, path):
        """Returns the HDPublicKey at the path indicated.
        Path should be in the form of m/x/y/z."""
        # start current node at self
        current = self
        # get components of the path split at '/', ignoring the first
        components = path.split("/")[1:]
        # iterate through the components
        for child in components:
            # raise a ValueError if the path ends with a '
            if child[-1:] == "'":
                raise ValueError("HDPublicKey cannot get hardened child")
            # traverse the next child at the index
            current = current.child(int(child))
        # return the current node
        return current

    def raw_serialize(self):
        if self._raw is None:
            if self.testnet:
                version = TESTNET_XPUB
            else:
                version = MAINNET_XPUB
            self._raw = self._serialize(version)
        return self._raw

    def _serialize(self, version):
        # start with the version
        raw = version
        # add the depth using int_to_byte
        raw += int_to_byte(self.depth)
        # add the parent_fingerprint
        raw += self.parent_fingerprint
        # add the child number in 4 bytes using int_to_big_endian
        raw += int_to_big_endian(self.child_number, 4)
        # add the chain code
        raw += self.chain_code
        # add the SEC pubkey
        raw += self.point.sec()
        return raw

    def _pub(self, version):
        """Returns the base58-encoded x/y/z pub.
        Expects a 4-byte version."""
        # get the serialization
        raw = self._serialize(version)
        # base58-encode the whole thing
        return encode_base58_checksum(raw)

    def xpub(self, version=None):

        # Allow for SLIP132 encoding (or other version bytes)
        if version is not None:
            return self._pub(version=version)

        if self.testnet:
            version = TESTNET_XPUB
        else:
            version = MAINNET_XPUB
        return self._pub(version)

    def ypub(self):
        if self.testnet:
            version = TESTNET_YPUB
        else:
            version = MAINNET_YPUB
        return self._pub(version)

    def zpub(self):
        if self.testnet:
            version = TESTNET_ZPUB
        else:
            version = MAINNET_ZPUB
        return self._pub(version)

    @classmethod
    def parse(cls, s):
        """Returns a HDPublicKey from an extended key string"""
        # get the bytes from the base58 using raw_decode_base58
        raw = raw_decode_base58(s)
        # check that the length of the raw is 78 bytes, otherwise raise ValueError
        if len(raw) != 78:
            raise ValueError("Not a proper extended key")
        # create a stream
        stream = BytesIO(raw)
        # return the raw parsing of the stream
        return cls.raw_parse(stream)

    @classmethod
    def raw_parse(cls, s):
        """Returns a HDPublicKey from a stream"""
        # first 4 bytes are the version
        version = s.read(4)
        # check that the version is one of the TESTNET or MAINNET
        #  public keys, if not raise a ValueError
        if version in (TESTNET_XPUB, TESTNET_YPUB, TESTNET_ZPUB):
            testnet = True
        elif version in (MAINNET_XPUB, MAINNET_YPUB, MAINNET_ZPUB):
            testnet = False
        else:
            raise ValueError("not an xpub, ypub or zpub: {} {}".format(s, version))
        # the next byte is depth
        depth = byte_to_int(s.read(1))
        # next 4 bytes are the parent_fingerprint
        parent_fingerprint = s.read(4)
        # next 4 bytes is the child number in big-endian
        child_number = big_endian_to_int(s.read(4))
        # next 32 bytes are the chain code
        chain_code = s.read(32)
        # last 33 bytes should be the SEC
        point = S256Point.parse(s.read(33))
        # return an instance of the class
        return cls(
            point=point,
            chain_code=chain_code,
            depth=depth,
            parent_fingerprint=parent_fingerprint,
            child_number=child_number,
            testnet=testnet,
        )
