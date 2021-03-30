from io import BytesIO

from buidl.ecc import S256Point, Signature
from buidl.hd import HDPublicKey
from buidl.helper import (
    base64_decode,
    base64_encode,
    child_to_path,
    encode_varstr,
    int_to_little_endian,
    little_endian_to_int,
    op_code_to_number,
    parse_binary_path,
    read_varint,
    read_varstr,
    serialize_binary_path,
    serialize_key_value,
)
from buidl.script import (
    RedeemScript,
    Script,
    WitnessScript,
)
from buidl.tx import Tx, TxOut
from buidl.witness import Witness


PSBT_MAGIC = b"\x70\x73\x62\x74"
PSBT_SEPARATOR = b"\xff"
PSBT_DELIMITER = b"\x00"
# PSBT global
PSBT_GLOBAL_UNSIGNED_TX = b"\x00"
PSBT_GLOBAL_XPUB = b"\x01"
# PSBT in
PSBT_IN_NON_WITNESS_UTXO = b"\x00"
PSBT_IN_WITNESS_UTXO = b"\x01"
PSBT_IN_PARTIAL_SIG = b"\x02"
PSBT_IN_SIGHASH_TYPE = b"\x03"
PSBT_IN_REDEEM_SCRIPT = b"\x04"
PSBT_IN_WITNESS_SCRIPT = b"\x05"
PSBT_IN_BIP32_DERIVATION = b"\x06"
PSBT_IN_FINAL_SCRIPTSIG = b"\x07"
PSBT_IN_FINAL_SCRIPTWITNESS = b"\x08"
PSBT_IN_POR_COMMITMENT = b"\x09"
# PSBT out
PSBT_OUT_REDEEM_SCRIPT = b"\x00"
PSBT_OUT_WITNESS_SCRIPT = b"\x01"
PSBT_OUT_BIP32_DERIVATION = b"\x02"


class NamedPublicKey(S256Point):
    def __repr__(self):
        return "Point:\n{}\nPath:\n{}:{}\n".format(
            self.sec().hex(), self.root_fingerprint.hex(), self.root_path
        )

    def add_raw_path_data(self, raw_path):
        self.root_fingerprint = raw_path[:4]
        self.root_path = parse_binary_path(raw_path[4:])
        self.raw_path = raw_path

    @classmethod
    def parse(cls, key, s):
        point = super().parse(key[1:])
        point.__class__ = cls
        point.add_raw_path_data(read_varstr(s))
        return point

    def serialize(self, prefix):
        return serialize_key_value(prefix + self.sec(), self.raw_path)


class NamedHDPublicKey(HDPublicKey):
    def __repr__(self):
        return "HD:\n{}\nPath:\n{}:{}\n".format(
            super().__repr__(), self.root_fingerprint.hex(), self.root_path
        )

    def add_raw_path_data(self, raw_path):
        self.root_fingerprint = raw_path[:4]
        bin_path = raw_path[4:]
        self.root_path = parse_binary_path(bin_path)
        if self.depth != len(bin_path) // 4:
            raise ValueError("raw path calculated depth and depth are different")
        self.raw_path = raw_path
        self.sync_point()

    def sync_point(self):
        self.point.__class__ = NamedPublicKey
        self.point.root_fingerprint = self.root_fingerprint
        self.point.root_path = self.root_path
        self.point.raw_path = self.raw_path

    def child(self, index):
        child = super().child(index)
        child.__class__ = self.__class__
        child.root_fingerprint = self.root_fingerprint
        child.root_path = self.root_path + child_to_path(index)
        child.raw_path = self.raw_path + int_to_little_endian(index, 4)
        child.sync_point()
        return child

    def pubkey_lookup(self, max_child=9):
        lookup = {}
        for child_index in range(max_child + 1):
            child = self.child(child_index)
            lookup[child.sec()] = child
            lookup[child.hash160()] = child
        return lookup

    def redeem_script_lookup(self, max_external=9, max_internal=9):
        """Returns a dictionary of RedeemScripts associated with p2sh-p2wpkh for the BIP44 child ScriptPubKeys"""
        # create a lookup to send back
        lookup = {}
        # create the external child (0)
        external = self.child(0)
        # loop through to the maximum external child + 1
        for child_index in range(max_external + 1):
            # grab the child at the index
            child = external.child(child_index)
            # create the p2sh-p2wpkh RedeemScript of [0, hash160]
            redeem_script = RedeemScript([0, child.hash160()])
            # hash160 of the RedeemScript is the key, RedeemScript is the value
            lookup[redeem_script.hash160()] = redeem_script
        # create the internal child (1)
        internal = self.child(1)
        # loop through to the maximum internal child + 1
        for child_index in range(max_internal + 1):
            # grab the child at the index
            child = internal.child(child_index)
            # create the p2sh-p2wpkh RedeemScript of [0, hash160]
            redeem_script = RedeemScript([0, child.hash160()])
            # hash160 of the RedeemScript is the key, RedeemScript is the value
            lookup[redeem_script.hash160()] = redeem_script
        # return the lookup
        return lookup

    def bip44_lookup(self, max_external=9, max_internal=9):
        external = self.child(0)
        internal = self.child(1)
        return {
            **external.pubkey_lookup(max_external),
            **internal.pubkey_lookup(max_internal),
        }

    @classmethod
    def parse(cls, key, s):
        hd_key = cls.raw_parse(BytesIO(key[1:]))
        hd_key.__class__ = cls
        hd_key.add_raw_path_data(read_varstr(s))
        return hd_key

    @classmethod
    def from_hd_priv(cls, hd_priv, path):
        hd_key = hd_priv.traverse(path).pub
        hd_key.__class__ = cls
        hd_key.add_raw_path_data(hd_priv.fingerprint() + serialize_binary_path(path))
        return hd_key

    def serialize(self):
        return serialize_key_value(
            PSBT_GLOBAL_XPUB + self.raw_serialize(), self.raw_path
        )

    def is_ancestor(self, named_pubkey):
        return named_pubkey.raw_path.startswith(self.raw_path)

    def verify_descendent(self, named_pubkey):
        if not self.is_ancestor(named_pubkey):
            raise ValueError("path is not a descendent of this key")
        remainder = named_pubkey.raw_path[len(self.raw_path) :]
        current = self
        while len(remainder):
            child_index = little_endian_to_int(remainder[:4])
            current = current.child(child_index)
            remainder = remainder[4:]
        return current.point == named_pubkey


class PSBT:
    def __init__(self, tx_obj, psbt_ins, psbt_outs, hd_pubs=None, extra_map=None):
        self.tx_obj = tx_obj
        self.psbt_ins = psbt_ins
        self.psbt_outs = psbt_outs
        self.hd_pubs = hd_pubs or {}
        self.extra_map = extra_map or {}
        self.validate()

    def validate(self):
        """Checks the PSBT for consistency"""
        if len(self.tx_obj.tx_ins) != len(self.psbt_ins):
            raise ValueError(
                "Number of psbt_ins in the transaction should match the psbt_ins array"
            )
        for i, psbt_in in enumerate(self.psbt_ins):
            # validate the input
            psbt_in.validate()
            tx_in = self.tx_obj.tx_ins[i]
            if tx_in.script_sig.commands:
                raise ValueError("ScriptSig for the tx should not be defined")
            # validate the ScriptSig
            if psbt_in.script_sig:
                tx_in.script_sig = psbt_in.script_sig
                tx_in.witness = psbt_in.witness
                if not self.tx_obj.verify_input(i):
                    raise ValueError(
                        "ScriptSig/Witness at input {} provided, but not valid".format(
                            i
                        )
                    )
                tx_in.script_sig = Script()
                tx_in.witness = Witness()
            # validate the signatures
            if psbt_in.sigs:
                for sec, sig in psbt_in.sigs.items():
                    point = S256Point.parse(sec)
                    signature = Signature.parse(sig[:-1])
                    if psbt_in.prev_tx:
                        # legacy
                        if not self.tx_obj.check_sig_legacy(
                            i, point, signature, psbt_in.redeem_script
                        ):
                            raise ValueError(
                                "legacy signature provided does not validate {}".format(
                                    self
                                )
                            )
                    elif psbt_in.prev_out:
                        # segwit
                        if not self.tx_obj.check_sig_segwit(
                            i,
                            point,
                            signature,
                            psbt_in.redeem_script,
                            psbt_in.witness_script,
                        ):
                            raise ValueError(
                                "segwit signature provided does not validate"
                            )
            # validate the NamedPublicKeys
            if psbt_in.named_pubs:
                for named_pub in psbt_in.named_pubs.values():
                    for hd_pub in self.hd_pubs.values():
                        if hd_pub.is_ancestor(named_pub):
                            if not hd_pub.verify_descendent(named_pub):
                                raise ValueError(
                                    "public key {} does not derive from xpub {}".format(
                                        named_pub, hd_pub
                                    )
                                )
                            break
        if len(self.tx_obj.tx_outs) != len(self.psbt_outs):
            raise ValueError(
                "Number of psbt_outs in the transaction should match the psbt_outs array"
            )
        for psbt_out in self.psbt_outs:
            # validate output
            psbt_out.validate()
            # validate the NamedPublicKeys
            if psbt_out.named_pubs:
                for named_pub in psbt_out.named_pubs.values():
                    for hd_pub in self.hd_pubs.values():
                        if hd_pub.is_ancestor(named_pub):
                            if not hd_pub.verify_descendent(named_pub):
                                raise ValueError(
                                    "public key {} does not derive from xpub {}".format(
                                        named_pub, hd_pub
                                    )
                                )
                            break
        return True

    def __repr__(self):
        return "Tx:\n{}\nPSBT XPUBS:\n{}\nPsbt_Ins:\n{}\nPsbt_Outs:\n{}\nExtra:{}\n".format(
            self.tx_obj, self.hd_pubs, self.psbt_ins, self.psbt_outs, self.extra_map
        )

    @classmethod
    def create(cls, tx_obj):
        """Create a PSBT from a transaction"""
        # create an array of PSBTIns
        psbt_ins = []
        # iterate through the inputs of the transaction
        for tx_in in tx_obj.tx_ins:
            # Empty ScriptSig and Witness fields
            # if ScriptSig exists, save it then empty it
            if tx_in.script_sig.commands:
                script_sig = tx_in.script_sig
                tx_in.script_sig = Script()
            else:
                script_sig = None
            # if Witness exists, save it then empty it
            if tx_in.witness:
                witness = tx_in.witness
                tx_in.witness = Witness()
            else:
                witness = None
            # Create a PSBTIn with the TxIn, ScriptSig and Witness
            psbt_in = PSBTIn(tx_in, script_sig=script_sig, witness=witness)
            # add PSBTIn to array
            psbt_ins.append(psbt_in)
        # create an array of PSBTOuts
        psbt_outs = []
        # iterate through the outputs of the transaction
        for tx_out in tx_obj.tx_outs:
            # create the PSBTOut with the TxOut
            psbt_out = PSBTOut(tx_out)
            # add PSBTOut to arary
            psbt_outs.append(psbt_out)
        # return an instance with the Tx, PSBTIn array and PSBTOut array
        return cls(tx_obj, psbt_ins, psbt_outs)

    def update(self, tx_lookup, pubkey_lookup, redeem_lookup=None, witness_lookup=None):
        if redeem_lookup is None:
            redeem_lookup = {}
        if witness_lookup is None:
            witness_lookup = {}
        # update each PSBTIn
        for psbt_in in self.psbt_ins:
            psbt_in.update(tx_lookup, pubkey_lookup, redeem_lookup, witness_lookup)
        # update each PSBTOut
        for psbt_out in self.psbt_outs:
            psbt_out.update(pubkey_lookup, redeem_lookup, witness_lookup)

    def sign(self, hd_priv):
        """Signs appropriate inputs with the hd private key provided"""
        # set the signed boolean to False until we sign something
        signed = False
        # grab the fingerprint of the private key
        fingerprint = hd_priv.fingerprint()
        # iterate through each PSBTIn
        for i, psbt_in in enumerate(self.psbt_ins):
            # iterate through the public keys associated with the PSBTIn
            for named_pub in psbt_in.named_pubs.values():
                # if the fingerprints match
                if named_pub.root_fingerprint == fingerprint:
                    # get the private key at the root_path of the NamedPublicKey
                    private_key = hd_priv.traverse(named_pub.root_path).private_key
                    # check if prev_tx is defined (legacy)
                    if psbt_in.prev_tx:
                        # get the signature using get_sig_legacy
                        sig = self.tx_obj.get_sig_legacy(
                            i, private_key, psbt_in.redeem_script
                        )
                        # update the sigs dict of the PSBTIn object
                        #  key is the sec and the value is the sig
                        psbt_in.sigs[private_key.point.sec()] = sig
                    # Exercise 4: check if prev_out is defined (segwit)
                    elif psbt_in.prev_out:
                        # get the signature using get_sig_segwit
                        sig = self.tx_obj.get_sig_segwit(
                            i,
                            private_key,
                            psbt_in.redeem_script,
                            psbt_in.witness_script,
                        )
                        # update the sigs dict of the PSBTIn object
                        #  key is the sec and the value is the sig
                        psbt_in.sigs[private_key.point.sec()] = sig
                    else:
                        raise ValueError("pubkey included without the previous output")
                    # set signed to True
                    signed = True
        # return whether we signed something
        return signed

    def sign_with_private_keys(self, private_keys):
        """Signs appropriate inputs with the hd private key provided"""
        # set the signed boolean to False until we sign something
        signed = False
        # iterate through each private key
        for private_key in private_keys:
            # grab the point associated with the point
            point = private_key.point
            # iterate through each PSBTIn
            for i, psbt_in in enumerate(self.psbt_ins):
                # if the sec is in the named_pubs dictionary
                if psbt_in.named_pubs.get(point.sec()):
                    if psbt_in.prev_tx:
                        # get the signature using get_sig_legacy
                        sig = self.tx_obj.get_sig_legacy(
                            i, private_key, psbt_in.redeem_script
                        )
                        # update the sigs dict of the PSBTIn object
                        #  key is the sec and the value is the sig
                        psbt_in.sigs[private_key.point.sec()] = sig
                    # Exercise 4: check if prev_out is defined (segwit)
                    elif psbt_in.prev_out:
                        # get the signature using get_sig_segwit
                        sig = self.tx_obj.get_sig_segwit(
                            i,
                            private_key,
                            psbt_in.redeem_script,
                            psbt_in.witness_script,
                        )
                        # update the sigs dict of the PSBTIn object
                        #  key is the sec and the value is the sig
                        psbt_in.sigs[private_key.point.sec()] = sig
                    else:
                        raise ValueError("pubkey included without the previous output")
                    # set signed to True
                    signed = True
        # return whether we signed something
        return signed

    def combine(self, other):
        """combines information from another PSBT to this one"""
        # the tx_obj properties should be the same or raise a ValueError
        if self.tx_obj.hash() != other.tx_obj.hash():
            raise ValueError(
                "cannot combine PSBTs that refer to different transactions"
            )
        # combine the hd_pubs
        self.hd_pubs = {**other.hd_pubs, **self.hd_pubs}
        # combine extra_map
        self.extra_map = {**other.extra_map, **self.extra_map}
        # combine psbt_ins
        for psbt_in_1, psbt_in_2 in zip(self.psbt_ins, other.psbt_ins):
            psbt_in_1.combine(psbt_in_2)
        # combine psbt_outs
        for psbt_out_1, psbt_out_2 in zip(self.psbt_outs, other.psbt_outs):
            psbt_out_1.combine(psbt_out_2)

    def finalize(self):
        """Finalize the transaction by filling in the ScriptSig and Witness fields for each input"""
        # iterate through the inputs
        for psbt_in in self.psbt_ins:
            # finalize each input
            psbt_in.finalize()

    def final_tx(self):
        """Returns the broadcast-able transaction"""
        # clone the transaction from self.tx_obj
        tx_obj = self.tx_obj.clone()
        # determine if the transaction is segwit by looking for a witness field
        #  in any PSBTIn. if so, set tx_obj.segwit = True
        if any([psbt_in.witness for psbt_in in self.psbt_ins]):
            tx_obj.segwit = True
        # iterate through the transaction and PSBT inputs together
        #  using zip(tx_obj.tx_ins, self.psbt_ins)
        for tx_in, psbt_in in zip(tx_obj.tx_ins, self.psbt_ins):
            # set the ScriptSig of the transaction input
            tx_in.script_sig = psbt_in.script_sig
            # Exercise 7: if the tx is segwit, set the witness as well
            if tx_obj.segwit:
                # witness should be the PSBTIn witness or an empty Witness()
                tx_in.witness = psbt_in.witness or Witness()
        # check to see that the transaction verifies
        if not tx_obj.verify():
            raise RuntimeError("transaction invalid")
        # return the now filled in transaction
        return tx_obj

    @classmethod
    def parse_base64(cls, b64):
        stream = BytesIO(base64_decode(b64))
        return cls.parse(stream)

    @classmethod
    def parse(cls, s):
        """Returns an instance of PSBT from a stream"""
        # prefix
        magic = s.read(4)
        if magic != PSBT_MAGIC:
            raise SyntaxError("Incorrect magic")
        separator = s.read(1)
        if separator != PSBT_SEPARATOR:
            raise SyntaxError("No separator")
        # global data
        tx_obj = None
        hd_pubs = {}
        extra_map = {}
        key = read_varstr(s)
        while key != b"":
            psbt_type = key[0:1]
            if psbt_type == PSBT_GLOBAL_UNSIGNED_TX:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if tx_obj:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                _ = read_varint(s)
                tx_obj = Tx.parse_legacy(s)
            elif psbt_type == PSBT_GLOBAL_XPUB:
                if len(key) != 79:
                    raise KeyError("Wrong length for the key")
                hd_pub = NamedHDPublicKey.parse(key, s)
                hd_pubs[hd_pub.raw_serialize()] = hd_pub
            else:
                if extra_map.get(key):
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                extra_map[key] = read_varstr(s)
            key = read_varstr(s)
        if not tx_obj:
            raise SyntaxError("transaction is required")
        # per input data
        psbt_ins = []
        for tx_in in tx_obj.tx_ins:
            psbt_ins.append(PSBTIn.parse(s, tx_in))
        # per output data
        psbt_outs = []
        for tx_out in tx_obj.tx_outs:
            psbt_outs.append(PSBTOut.parse(s, tx_out))
        return cls(tx_obj, psbt_ins, psbt_outs, hd_pubs, extra_map)

    def serialize_base64(self):
        return base64_encode(self.serialize())

    def serialize(self):
        # always start with the magic and separator
        result = PSBT_MAGIC + PSBT_SEPARATOR
        # tx
        result += serialize_key_value(PSBT_GLOBAL_UNSIGNED_TX, self.tx_obj.serialize())
        # xpubs
        for xpub in sorted(self.hd_pubs.keys()):
            hd_pub = self.hd_pubs[xpub]
            result += hd_pub.serialize()
        for key in sorted(self.extra_map.keys()):
            result += serialize_key_value(key, self.extra_map[key])
        # delimiter
        result += PSBT_DELIMITER
        # per input data
        for psbt_in in self.psbt_ins:
            result += psbt_in.serialize()
        # per output data
        for psbt_out in self.psbt_outs:
            result += psbt_out.serialize()
        return result


class PSBTIn:
    def __init__(
        self,
        tx_in,
        prev_tx=None,
        prev_out=None,
        sigs=None,
        hash_type=None,
        redeem_script=None,
        witness_script=None,
        named_pubs=None,
        script_sig=None,
        witness=None,
        extra_map=None,
    ):
        self.tx_in = tx_in
        self.prev_tx = prev_tx
        self.prev_out = prev_out
        if self.prev_tx and self.prev_out:
            raise ValueError(
                "only one of prev_tx and prev_out should be defined: {} {}".format(
                    prev_tx, prev_out
                )
            )
        self.sigs = sigs or {}
        self.hash_type = hash_type
        self.redeem_script = redeem_script
        self.witness_script = witness_script
        self.named_pubs = named_pubs or {}
        self.script_sig = script_sig
        self.witness = witness
        self.extra_map = extra_map or {}
        self.validate()

    def validate(self):
        """Checks the PSBTIn for consistency"""
        script_pubkey = self.script_pubkey()
        if self.prev_tx:
            if self.tx_in.prev_tx != self.prev_tx.hash():
                raise ValueError(
                    "previous transaction specified, but does not match the input tx"
                )
            if self.tx_in.prev_index >= len(self.prev_tx.tx_outs):
                raise ValueError("input refers to an output index that does not exist")
            if self.redeem_script:
                if not script_pubkey.is_p2sh():
                    raise ValueError("RedeemScript defined for non-p2sh ScriptPubKey")
                # non-witness p2sh
                if self.redeem_script.is_p2wsh() or self.redeem_script.is_p2wpkh():
                    raise ValueError("Non-witness UTXO provided for witness input")
                h160 = script_pubkey.commands[1]
                if self.redeem_script.hash160() != h160:
                    raise ValueError(
                        "RedeemScript hash160 and ScriptPubKey hash160 do not match"
                    )
                for sec in self.named_pubs.keys():
                    try:
                        # this will raise a ValueError if it's not in there
                        self.redeem_script.commands.index(sec)
                    except ValueError:
                        raise ValueError(
                            "pubkey is not in RedeemScript {}".format(self)
                        )
            elif script_pubkey.is_p2pkh():
                if len(self.named_pubs) > 1:
                    raise ValueError("too many pubkeys in p2pkh")
                elif len(self.named_pubs) == 1:
                    named_pub = list(self.named_pubs.values())[0]
                    if script_pubkey.commands[2] != named_pub.hash160():
                        raise ValueError(
                            "pubkey {} does not match the hash160".format(
                                named_pub.sec().hex()
                            )
                        )
        elif self.prev_out:
            if (
                not script_pubkey.is_p2sh()
                and not script_pubkey.is_p2wsh()
                and not script_pubkey.is_p2wpkh()
            ):
                raise ValueError("Witness UTXO provided for non-witness input")
            if self.witness_script:  # p2wsh or p2sh-p2wsh
                if not script_pubkey.is_p2wsh() and not (
                    self.redeem_script and self.redeem_script.is_p2wsh()
                ):
                    raise ValueError(
                        "WitnessScript provided for non-p2wsh ScriptPubKey"
                    )
                if self.redeem_script:
                    h160 = script_pubkey.commands[1]
                    if self.redeem_script.hash160() != h160:
                        raise ValueError(
                            "RedeemScript hash160 and ScriptPubKey hash160 do not match"
                        )
                    s256 = self.redeem_script.commands[1]
                else:
                    s256 = self.prev_out.script_pubkey.commands[1]
                if self.witness_script.sha256() != s256:
                    raise ValueError(
                        "WitnessScript sha256 and output sha256 do not match"
                    )
                for sec in self.named_pubs.keys():
                    try:
                        # this will raise a ValueError if it's not in there
                        self.witness_script.commands.index(sec)
                    except ValueError:
                        raise ValueError(
                            "pubkey is not in WitnessScript: {}".format(self)
                        )
            elif script_pubkey.is_p2wpkh() or (
                self.redeem_script and self.redeem_script.is_p2wpkh()
            ):
                if len(self.named_pubs) > 1:
                    raise ValueError("too many pubkeys in p2wpkh or p2sh-p2wpkh")
                elif len(self.named_pubs) == 1:
                    named_pub = list(self.named_pubs.values())[0]
                    if script_pubkey.commands[1] != named_pub.hash160():
                        raise ValueError(
                            "pubkey {} does not match the hash160".format(
                                named_pub.sec().hex()
                            )
                        )

    def __repr__(self):
        return "TxIn:\n{}\nPrev Tx:\n{}\nPrev Output\n{}\nSigs:\n{}\nRedeemScript:\n{}\nWitnessScript:\n{}\nPSBT Pubs:\n{}\nScriptSig:\n{}\nWitness:\n{}\n".format(
            self.tx_in,
            self.prev_tx,
            self.prev_out,
            self.sigs,
            self.redeem_script,
            self.witness_script,
            self.named_pubs,
            self.script_sig,
            self.witness,
        )

    @classmethod
    def parse(cls, s, tx_in):
        prev_tx = None
        prev_out = None
        sigs = {}
        hash_type = None
        redeem_script = None
        witness_script = None
        named_pubs = {}
        script_sig = None
        witness = None
        extra_map = {}
        key = read_varstr(s)
        while key != b"":
            psbt_type = key[0:1]
            if psbt_type == PSBT_IN_NON_WITNESS_UTXO:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if prev_tx:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                tx_len = read_varint(s)
                prev_tx = Tx.parse(s)
                if len(prev_tx.serialize()) != tx_len:
                    raise IOError("tx length does not match")
                tx_in._value = prev_tx.tx_outs[tx_in.prev_index].amount
                tx_in._script_pubkey = prev_tx.tx_outs[tx_in.prev_index].script_pubkey
            elif psbt_type == PSBT_IN_WITNESS_UTXO:
                tx_out_len = read_varint(s)
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if prev_out:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                prev_out = TxOut.parse(s)
                if len(prev_out.serialize()) != tx_out_len:
                    raise ValueError("tx out length does not match")
                tx_in._value = prev_out.amount
                tx_in._script_pubkey = prev_out.script_pubkey
            elif psbt_type == PSBT_IN_PARTIAL_SIG:
                if sigs.get(key[1:]):
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                sigs[key[1:]] = read_varstr(s)
            elif psbt_type == PSBT_IN_SIGHASH_TYPE:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if hash_type:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                hash_type = little_endian_to_int(read_varstr(s))
            elif psbt_type == PSBT_IN_REDEEM_SCRIPT:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if redeem_script:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                redeem_script = RedeemScript.parse(s)
            elif psbt_type == PSBT_IN_WITNESS_SCRIPT:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if witness_script:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                witness_script = WitnessScript.parse(s)
            elif psbt_type == PSBT_IN_BIP32_DERIVATION:
                if len(key) != 34:
                    raise KeyError("Wrong length for the key")
                named_pub = NamedPublicKey.parse(key, s)
                named_pubs[named_pub.sec()] = named_pub
            elif psbt_type == PSBT_IN_FINAL_SCRIPTSIG:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if script_sig:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                script_sig = Script.parse(s)
            elif psbt_type == PSBT_IN_FINAL_SCRIPTWITNESS:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if witness:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                _ = read_varint(s)
                witness = Witness.parse(s)
            else:
                if extra_map.get(key):
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                extra_map[key] = read_varstr(s)
            key = read_varstr(s)
        return cls(
            tx_in,
            prev_tx,
            prev_out,
            sigs,
            hash_type,
            redeem_script,
            witness_script,
            named_pubs,
            script_sig,
            witness,
            extra_map,
        )

    def serialize(self):
        result = b""
        if self.prev_tx:
            result += serialize_key_value(
                PSBT_IN_NON_WITNESS_UTXO, self.prev_tx.serialize()
            )
        elif self.prev_out:
            result += serialize_key_value(
                PSBT_IN_WITNESS_UTXO, self.prev_out.serialize()
            )
        # we need to put the keys in the witness script or redeem script order
        keys = []
        if self.witness_script:
            for command in self.witness_script.commands:
                if self.sigs.get(command):
                    keys.append(command)
        elif self.redeem_script and not self.redeem_script.is_p2wpkh():
            for command in self.redeem_script.commands:
                if self.sigs.get(command):
                    keys.append(command)
        else:
            keys = sorted(self.sigs.keys())
        for key in keys:
            result += serialize_key_value(PSBT_IN_PARTIAL_SIG + key, self.sigs[key])
        if self.hash_type:
            result += serialize_key_value(
                PSBT_IN_SIGHASH_TYPE, int_to_little_endian(self.hash_type, 4)
            )
        if self.redeem_script:
            result += serialize_key_value(
                PSBT_IN_REDEEM_SCRIPT, self.redeem_script.raw_serialize()
            )
        if self.witness_script:
            result += serialize_key_value(
                PSBT_IN_WITNESS_SCRIPT, self.witness_script.raw_serialize()
            )
        for sec in sorted(self.named_pubs.keys()):
            named_pub = self.named_pubs[sec]
            result += named_pub.serialize(PSBT_IN_BIP32_DERIVATION)
        if self.script_sig:
            result += serialize_key_value(
                PSBT_IN_FINAL_SCRIPTSIG, self.script_sig.raw_serialize()
            )
        if self.witness:
            result += serialize_key_value(
                PSBT_IN_FINAL_SCRIPTWITNESS, self.witness.serialize()
            )
        # extra
        for key in sorted(self.extra_map.keys()):
            result += encode_varstr(key) + encode_varstr(self.extra_map[key])
        # delimiter
        result += PSBT_DELIMITER
        return result

    def script_pubkey(self):
        if self.prev_tx:
            return self.prev_tx.tx_outs[self.tx_in.prev_index].script_pubkey
        elif self.prev_out:
            return self.prev_out.script_pubkey
        else:
            return None

    def update(self, tx_lookup, pubkey_lookup, redeem_lookup, witness_lookup):
        """Updates the input with NamedPublicKeys, RedeemScript or WitnessScript that
        correspond"""
        # the input might already have a previous transaction
        prev_tx = self.prev_tx or tx_lookup.get(self.tx_in.prev_tx)
        # grab the output at the previous index, or alternatively get the self.prev_out
        if prev_tx:
            prev_out = prev_tx.tx_outs[self.tx_in.prev_index]
        else:
            prev_out = self.prev_out
        # if we don't know the previous output we can't update anything
        if not prev_tx and not prev_out:
            return
        # get the ScriptPubKey that we're unlocking
        script_pubkey = prev_out.script_pubkey
        # Set the _value and _script_pubkey properties of the TxIn object
        #  so that no full node is needed to look those up
        self.tx_in._value = prev_out.amount
        self.tx_in._script_pubkey = script_pubkey
        # grab the RedeemScript
        if script_pubkey.is_p2sh():
            # see if we have a RedeemScript already defined or in the lookup
            self.redeem_script = self.redeem_script or redeem_lookup.get(
                script_pubkey.commands[1]
            )
            # if there's no RedeemScript, we can't do any more updating, so return
            if not self.redeem_script:
                return
        # Exercise 2: if we have p2wpkh or p2sh-p2wpkh see if we have the appropriate NamedPublicKey
        if script_pubkey.is_p2wpkh() or (
            self.redeem_script and self.redeem_script.is_p2wpkh()
        ):
            # set the prev_out property as this is Segwit
            self.prev_out = prev_out
            # for p2wpkh, the hash160 is the second command of the ScriptPubKey
            # for p2sh-p2wpkh, the hash160 is the second command of the RedeemScript
            if script_pubkey.is_p2wpkh():
                h160 = script_pubkey.commands[1]
            else:
                h160 = self.redeem_script.commands[1]
            # see if we have the public key that corresponds to the hash160
            named_pub = pubkey_lookup.get(h160)
            # if so add it to the named_pubs dictionary
            if named_pub:
                self.named_pubs[named_pub.sec()] = named_pub.point
        # Exercise 12: if we have p2wsh or p2sh-p2wsh see if we have one or more NamedPublicKeys
        elif script_pubkey.is_p2wsh() or (
            self.redeem_script and self.redeem_script.is_p2wsh()
        ):
            # set the prev_out property as this is Segwit
            self.prev_out = prev_out
            # for p2wsh, the sha256 is the second command of the ScriptPubKey
            # for p2sh-p2wsh, the sha256 is the second command of the RedeemScript
            if script_pubkey.is_p2wsh():
                s256 = script_pubkey.commands[1]
            else:
                s256 = self.redeem_script.commands[1]
            # see if we have the WitnessScript that corresponds to the sha256
            self.witness_script = self.witness_script or witness_lookup.get(s256)
            if self.witness_script:
                # go through the commands of the WitnessScript for NamedPublicKeys
                for command in self.witness_script.commands:
                    named_pub = pubkey_lookup.get(command)
                    if named_pub:
                        self.named_pubs[named_pub.sec()] = named_pub.point
        # we've eliminated p2sh wrapped segwit, handle p2sh here
        elif script_pubkey.is_p2sh():
            # set the prev_tx property as it's not segwit
            self.prev_tx = prev_tx
            # go through the commands of the RedeemScript for NamedPublicKeys
            for command in self.redeem_script.commands:
                # if we find a NamedPublicKey, add to the named_pubs dictionary
                #  key is compressed sec, value is the point object
                named_pub = pubkey_lookup.get(command)
                if named_pub:
                    self.named_pubs[named_pub.sec()] = named_pub.point
        # if we have p2pkh, see if we have the appropriate NamedPublicKey
        elif script_pubkey.is_p2pkh():
            # set the prev_tx property as it's not segwit
            self.prev_tx = prev_tx
            # look for the NamedPublicKey that corresponds to the hash160
            #  which is the 3rd command of the ScriptPubKey
            named_pub = pubkey_lookup.get(script_pubkey.commands[2])
            if named_pub:
                # if it exists, add to the named_pubs dict
                #  key is the sec and the value is the point
                self.named_pubs[named_pub.sec()] = named_pub.point
        # else we throw a ValueError
        else:
            raise ValueError(
                "cannot update a transaction because it is not p2pkh, p2sh, p2wpkh or p2wsh: {}".format(
                    script_pubkey
                )
            )

    def combine(self, other):
        """Combines two PSBTIn objects into self"""
        # if prev_tx is defined in the other, but not in self, add
        if self.prev_tx is None and other.prev_tx:
            self.prev_tx = other.prev_tx
        # if prev_tx is defined in the other, but not in self, add
        if self.prev_out is None and other.prev_out:
            self.prev_out = other.prev_out
        # combine the sigs
        self.sigs = {**self.sigs, **other.sigs}
        # if hash_type is defined in the other, but not in self, add
        if self.hash_type is None and other.hash_type:
            self.hash_type = other.hash_type
        # if redeem_script is defined in the other, but not in self, add
        if self.redeem_script is None and other.redeem_script:
            self.redeem_script = other.redeem_script
        # if witness_script is defined in the other, but not in self, add
        if self.witness_script is None and other.witness_script:
            self.witness_script = other.witness_script
        # combine the pubs
        self.named_pubs = {**other.named_pubs, **self.named_pubs}
        # if script_sig is defined in the other, but not in self, add
        if self.script_sig is None and other.script_sig:
            self.script_sig = other.script_sig
        # if witness is defined in the other, but not in self, add
        if self.witness is None and other.witness:
            self.witness = other.witness
        # combine extra_map
        self.extra_map = {**other.extra_map, **self.extra_map}

    def finalize(self):
        """Removes all sigs/named pubs/RedeemScripts/WitnessScripts and
        sets the script_sig and witness fields"""
        # get the ScriptPubKey for this input
        script_pubkey = self.script_pubkey()
        # if the ScriptPubKey is p2sh
        if script_pubkey.is_p2sh():
            # make sure there's a RedeemScript
            if not self.redeem_script:
                raise RuntimeError("Cannot finalize p2sh without a RedeemScript")
        # Exercise 6: if p2wpkh or p2sh-p2wpkh
        if script_pubkey.is_p2wpkh() or (
            self.redeem_script and self.redeem_script.is_p2wpkh()
        ):
            # check to see that we have exactly 1 signature
            if len(self.sigs) != 1:
                raise RuntimeError(
                    "p2wpkh or p2sh-p2wpkh should have exactly 1 signature"
                )
            # the key of the sigs dict is the compressed SEC pubkey
            sec = list(self.sigs.keys())[0]
            # the value of the sigs dict is the signature
            sig = list(self.sigs.values())[0]
            # set the ScriptSig to the RedeemScript if there is one
            if self.redeem_script:
                self.script_sig = Script([self.redeem_script.raw_serialize()])
            else:
                self.script_sig = Script()
            # set the Witness to sig and sec
            self.witness = Witness([sig, sec])
        # Exercise 15: if p2wsh or p2sh-p2wsh
        elif script_pubkey.is_p2wsh() or (
            self.redeem_script and self.redeem_script.is_p2wsh()
        ):
            # make sure there's a WitnessScript
            if not self.witness_script:
                raise RuntimeError(
                    "Cannot finalize p2wsh or p2sh-p2wsh without a WitnessScript"
                )
            # convert the first command to a number (required # of sigs)
            num_sigs = op_code_to_number(self.witness_script.commands[0])
            # make sure we have at least the number of sigs required
            if len(self.sigs) < num_sigs:
                raise RuntimeError(
                    "Cannot finalize p2wsh or p2sh-p2wsh because {} sigs were provided where {} were needed".format(
                        len(self.sigs), num_sigs
                    )
                )
            # create a list of items for the Witness. Start with b'\x00' for the
            #  OP_CHECKMULTISIG off-by-one error
            witness_items = [b"\x00"]
            # for each command in the WitnessScript
            for command in self.witness_script.commands:
                # grab the sig for the pubkey
                sig = self.sigs.get(command)
                # if the sig exists, then add to the Witness item list
                if sig is not None:
                    witness_items.append(sig)
                # when we have enough signatures, break
                if len(witness_items) - 1 >= num_sigs:
                    break
            # make sure we have enough sigs to pass validation
            if len(witness_items) - 1 < num_sigs:
                raise RuntimeError("Not enough signatures provided for p2sh-p2wsh")
            # add the raw WitnessScript as the last item for p2wsh execution
            witness_items.append(self.witness_script.raw_serialize())
            # create the witness
            self.witness = Witness(witness_items)
            # set the ScriptSig to the RedeemScript if there is one
            if self.redeem_script:
                self.script_sig = Script([self.redeem_script.raw_serialize()])
            else:
                self.script_sig = Script()
        # we've eliminated p2sh wrapped segwit, handle p2sh here
        elif script_pubkey.is_p2sh():
            # convert the first command to a number (required # of sigs)
            num_sigs = op_code_to_number(self.redeem_script.commands[0])
            # make sure we have at least the number of sigs required
            if len(self.sigs) < num_sigs:
                raise RuntimeError(
                    "Cannot finalize p2sh because {} sigs were provided where {} were needed".format(
                        len(self.sigs), num_sigs
                    )
                )
            # create a list of commands for the ScriptSig. Start with 0 for the
            #  OP_CHECKMULTISIG off-by-one error
            script_sig_commands = [0]
            # for each command in the RedeemScript
            for command in self.redeem_script.commands:
                # skip if the command is an integer
                if type(command) == int:
                    continue
                # grab the sig for the pubkey
                sig = self.sigs.get(command)
                # if the sig exists, then add to the ScriptSig command list
                if sig is not None:
                    script_sig_commands.append(sig)
                # when we have enough signatures, break
                if len(script_sig_commands) - 1 >= num_sigs:
                    break
            # make sure we have enough sigs to pass validation
            if len(script_sig_commands) < num_sigs:
                raise RuntimeError("Not enough signatures provided for p2wsh")
            # add the raw redeem script as the last command for p2sh execution
            script_sig_commands.append(self.redeem_script.raw_serialize())
            # change the ScriptSig to be a Script with the commands we've gathered
            self.script_sig = Script(script_sig_commands)
        elif script_pubkey.is_p2pkh():
            # check to see that we have exactly 1 signature
            if len(self.sigs) != 1:
                raise RuntimeError("P2pkh requires exactly 1 signature")
            # the key of the sigs dict is the compressed SEC pubkey
            sec = list(self.sigs.keys())[0]
            # the value of the sigs dict is the signature
            sig = list(self.sigs.values())[0]
            # set the ScriptSig, which is Script([sig, sec])
            self.script_sig = Script([sig, sec])
        else:
            raise ValueError(
                "Cannot finalize this ScriptPubKey: {}".format(script_pubkey)
            )
        # reset sigs, hash_type, redeem_script, witness_script and named_pubs to be empty
        self.sigs = {}
        self.hash_type = None
        self.redeem_script = None
        self.witness_script = None
        self.named_pubs = {}


class PSBTOut:
    def __init__(
        self,
        tx_out,
        redeem_script=None,
        witness_script=None,
        named_pubs=None,
        extra_map=None,
    ):
        self.tx_out = tx_out
        self.redeem_script = redeem_script
        self.witness_script = witness_script
        self.named_pubs = named_pubs or {}
        self.extra_map = extra_map or {}
        self.validate()

    def validate(self):
        """Checks the PSBTOut for consistency"""
        script_pubkey = self.tx_out.script_pubkey
        if script_pubkey.is_p2pkh():
            if self.redeem_script:
                raise KeyError("RedeemScript included in p2pkh output")
            if self.witness_script:
                raise KeyError("WitnessScript included in p2pkh output")
            if len(self.named_pubs) > 1:
                raise ValueError("too many pubkeys in p2pkh")
            elif len(self.named_pubs) == 1:
                named_pub = list(self.named_pubs.values())[0]
                if script_pubkey.commands[2] != named_pub.hash160():
                    raise ValueError(
                        "pubkey {} does not match the hash160".format(
                            named_pub.sec().hex()
                        )
                    )
        elif script_pubkey.is_p2wpkh():
            if self.redeem_script:
                raise KeyError("RedeemScript included in p2wpkh output")
            if self.witness_script:
                raise KeyError("WitnessScript included in p2wpkh output")
            if len(self.named_pubs) > 1:
                raise ValueError("too many pubkeys in p2wpkh")
            elif len(self.named_pubs) == 1:
                named_pub = list(self.named_pubs.values())[0]
                if script_pubkey.commands[1] != named_pub.hash160():
                    raise ValueError(
                        "pubkey {} does not match the hash160".format(
                            named_pub.sec().hex()
                        )
                    )
        elif self.witness_script:
            if self.redeem_script:
                h160 = script_pubkey.commands[1]
                if self.redeem_script.hash160() != h160:
                    raise ValueError(
                        "RedeemScript hash160 and ScriptPubKey hash160 do not match"
                    )
                s256 = self.redeem_script.commands[1]
            else:
                s256 = script_pubkey.commands[1]
            if self.witness_script.sha256() != s256:
                raise ValueError(
                    "WitnessScript sha256 and output sha256 do not match {} {}".format(
                        self, self.witness_script.sha256().hex()
                    )
                )
            for sec in self.named_pubs.keys():
                try:
                    # this will raise a ValueError if it's not in there
                    self.witness_script.commands.index(sec)
                except ValueError:
                    raise ValueError("pubkey is not in WitnessScript {}".format(self))
        elif self.redeem_script:
            for sec in self.named_pubs.keys():
                try:
                    # this will raise a ValueError if it's not in there
                    self.redeem_script.commands.index(sec)
                except ValueError:
                    raise ValueError("pubkey is not in RedeemScript {}".format(self))

    def __repr__(self):
        return (
            "TxOut:\n{}\nRedeemScript:\n{}\nWitnessScript\n{}\nPSBT Pubs:\n{}\n".format(
                self.tx_out, self.redeem_script, self.witness_script, self.named_pubs
            )
        )

    @classmethod
    def parse(cls, s, tx_out):
        redeem_script = None
        witness_script = None
        named_pubs = {}
        extra_map = {}
        key = read_varstr(s)
        while key != b"":
            psbt_type = key[0:1]
            if psbt_type == PSBT_OUT_REDEEM_SCRIPT:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if redeem_script:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                redeem_script = RedeemScript.parse(s)
            elif psbt_type == PSBT_OUT_WITNESS_SCRIPT:
                if len(key) != 1:
                    raise KeyError("Wrong length for the key")
                if witness_script:
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                witness_script = WitnessScript.parse(s)
            elif psbt_type == PSBT_OUT_BIP32_DERIVATION:
                if len(key) != 34:
                    raise KeyError("Wrong length for the key")
                named_pub = NamedPublicKey.parse(key, s)
                named_pubs[named_pub.sec()] = named_pub
            else:
                if extra_map.get(key):
                    raise KeyError("Duplicate Key in parsing: {}".format(key.hex()))
                extra_map[key] = read_varstr(s)
            key = read_varstr(s)
        return cls(tx_out, redeem_script, witness_script, named_pubs, extra_map)

    def serialize(self):
        result = b""
        if self.redeem_script:
            result += serialize_key_value(
                PSBT_OUT_REDEEM_SCRIPT, self.redeem_script.raw_serialize()
            )
        if self.witness_script:
            result += serialize_key_value(
                PSBT_OUT_WITNESS_SCRIPT, self.witness_script.raw_serialize()
            )
        for key in sorted(self.named_pubs.keys()):
            named_pub = self.named_pubs[key]
            result += named_pub.serialize(PSBT_OUT_BIP32_DERIVATION)
        # extra
        for key in sorted(self.extra_map.keys()):
            result += encode_varstr(key) + encode_varstr(self.extra_map[key])
        # delimiter
        result += PSBT_DELIMITER
        return result

    def update(self, pubkey_lookup, redeem_lookup, witness_lookup):
        """Updates the output with NamedPublicKeys, RedeemScript or WitnessScript that
        correspond"""
        # get the ScriptPubKey
        script_pubkey = self.tx_out.script_pubkey
        # if the ScriptPubKey is p2sh, check for a RedeemScript
        if script_pubkey.is_p2sh():
            self.redeem_script = redeem_lookup.get(script_pubkey.commands[1])
            # if no RedeemScript exists, we can't update, so return
            if not self.redeem_script:
                return
        # Exercise 2: if p2wpkh or p2sh-p2wpkh
        if script_pubkey.is_p2wpkh() or (
            self.redeem_script and self.redeem_script.is_p2wpkh()
        ):
            # get the hash160 (second command of RedeemScript or ScriptPubKey)
            if self.redeem_script:
                h160 = self.redeem_script.commands[1]
            else:
                h160 = script_pubkey.commands[1]
            # look for the NamedPublicKey and add if there
            named_pub = pubkey_lookup.get(h160)
            if named_pub:
                self.named_pubs[named_pub.sec()] = named_pub.point
        # Exercise 12: if p2wsh/p2sh-p2wsh
        elif script_pubkey.is_p2wsh() or (
            self.redeem_script and self.redeem_script.is_p2wsh()
        ):
            # get the sha256 (second command of RedeemScript or ScriptPubKey)
            if self.redeem_script:
                s256 = self.redeem_script.commands[1]
            else:
                s256 = script_pubkey.commands[1]
            # look for the WitnessScript using the sha256
            witness_script = witness_lookup.get(s256)
            if witness_script:
                # update the WitnessScript
                self.witness_script = witness_script
                # look through the WitnessScript for any NamedPublicKeys
                for command in witness_script.commands:
                    named_pub = pubkey_lookup.get(command)
                    # if found, add the NamedPublicKey
                    if named_pub:
                        self.named_pubs[named_pub.sec()] = named_pub.point
        # we've eliminated p2sh wrapped segwit, handle p2sh here
        elif script_pubkey.is_p2sh():
            # Look through the commands in the RedeemScript for any NamedPublicKeys
            for command in self.redeem_script.commands:
                named_pub = pubkey_lookup.get(command)
                # if a NamedPublicKey exists
                if named_pub:
                    # add to the named_pubs dictionary
                    #  key is sec and the point is the value
                    self.named_pubs[named_pub.sec()] = named_pub.point
        # Exercise 3: if the ScriptPubKey is p2pkh,
        elif script_pubkey.is_p2pkh():
            # Look at the third command of the ScriptPubKey for the hash160
            # Use that to look up the NamedPublicKey
            named_pub = pubkey_lookup.get(script_pubkey.commands[2])
            # if a NamedPublicKey exists
            if named_pub:
                # add to the named_pubs dictionary
                #  key is sec and the point is the value
                self.named_pubs[named_pub.sec()] = named_pub.point

    def combine(self, other):
        """Combines two PSBTOuts to self"""
        # if redeem_script is defined in the other, but not in self, add
        if self.redeem_script is None and other.redeem_script:
            self.redeem_script = other.redeem_script
        # if witness_script is defined in the other, but not in self, add
        if self.witness_script is None and other.witness_script:
            self.witness_script = other.witness_script
        # combine the pubs
        self.named_pubs = {**other.named_pubs, **self.named_pubs}
        # combine extra_map
        self.extra_map = {**other.extra_map, **self.extra_map}
