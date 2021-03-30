from io import BytesIO

from buidl.helper import (
    byte_to_int,
    encode_base58_checksum,
    encode_bech32_checksum,
    encode_varstr,
    hash160,
    int_to_byte,
    int_to_little_endian,
    read_varint,
    sha256,
)
from buidl.op import (
    op_equal,
    op_hash160,
    op_verify,
    OP_CODE_FUNCTIONS,
    OP_CODE_NAMES,
)


class Script:
    def __init__(self, commands=None):
        if commands is None:
            self.commands = []
        else:
            self.commands = commands

    def __repr__(self):
        result = ""
        for command in self.commands:
            if type(command) == int:
                if OP_CODE_NAMES.get(command):
                    name = OP_CODE_NAMES.get(command)
                else:
                    name = "OP_[{}]".format(command)
                result += "{} ".format(name)
            else:
                result += "{} ".format(command.hex())
        return result

    def __eq__(self, other):
        return self.commands == other.commands

    def __add__(self, other):
        return Script(self.commands + other.commands)

    @classmethod
    def parse(cls, s):
        # get the length of the entire field
        length = read_varint(s)
        # initialize the commands array
        commands = []
        # initialize the number of bytes we've read to 0
        count = 0
        # loop until we've read length bytes
        while count < length:
            # get the current byte
            current = s.read(1)
            # increment the bytes we've read
            count += 1
            # convert the current byte to an integer
            current_byte = current[0]
            # if the current byte is between 1 and 75 inclusive
            if current_byte >= 1 and current_byte <= 75:
                # we have a command set n to be the current byte
                n = current_byte
                # add the next n bytes as a command
                commands.append(s.read(n))
                # increase the count by n
                count += n
            elif current_byte == 76:
                # op_pushdata1
                data_length = byte_to_int(s.read(1))
                commands.append(s.read(data_length))
                count += data_length + 1
            elif current_byte == 77:
                # op_pushdata2
                data_length = byte_to_int(s.read(2))
                commands.append(s.read(data_length))
                count += data_length + 2
            else:
                # we have an op code. set the current byte to op_code
                op_code = current_byte
                # add the op_code to the list of commands
                commands.append(op_code)
        if count != length:
            raise SyntaxError("parsing script failed")
        return cls(commands)

    def raw_serialize(self):
        # initialize what we'll send back
        result = b""
        # go through each command
        for command in self.commands:
            # if the command is an integer, it's an op code
            if type(command) == int:
                # turn the command into a single byte integer using int_to_byte
                result += int_to_byte(command)
            else:
                # otherwise, this is an element
                # get the length in bytes
                length = len(command)
                # for large lengths, we have to use a pushdata op code
                if length < 75:
                    # turn the length into a single byte integer
                    result += int_to_byte(length)
                elif length > 75 and length < 0x100:
                    # 76 is pushdata1
                    result += int_to_byte(76)
                    result += int_to_byte(length)
                elif length >= 0x100 and length <= 520:
                    # 77 is pushdata2
                    result += int_to_byte(77)
                    result += int_to_little_endian(length, 2)
                else:
                    raise ValueError("too long a command")
                result += command
        return result

    def serialize(self):
        # get the raw serialization (no prepended length)
        result = self.raw_serialize()
        # encode_varstr the result
        return encode_varstr(result)

    def evaluate(self, z, witness):
        # create a copy as we may need to add to this list if we have a
        # RedeemScript
        commands = self.commands[:]
        stack = []
        altstack = []
        while len(commands) > 0:
            command = commands.pop(0)
            if type(command) == int:
                # do what the op code says
                operation = OP_CODE_FUNCTIONS[command]
                if command in (99, 100):
                    # op_if/op_notif require the commands array
                    if not operation(stack, commands):
                        print("bad op: {}".format(OP_CODE_NAMES[command]))
                        return False
                elif command in (107, 108):
                    # op_toaltstack/op_fromaltstack require the altstack
                    if not operation(stack, altstack):
                        print("bad op: {}".format(OP_CODE_NAMES[command]))
                        return False
                elif command in (172, 173, 174, 175):
                    # these are signing operations, they need a sig_hash
                    # to check against
                    if not operation(stack, z):
                        print("bad op: {}".format(OP_CODE_NAMES[command]))
                        return False
                else:
                    if not operation(stack):
                        print("bad op: {}".format(OP_CODE_NAMES[command]))
                        return False
            else:
                # add the command to the stack
                stack.append(command)
                # p2sh rule. if the next three commands are:
                # OP_HASH160 <20 byte hash> OP_EQUAL this is the RedeemScript
                # OP_HASH160 == 0xa9 and OP_EQUAL == 0x87
                if (
                    len(commands) == 3
                    and commands[0] == 0xA9
                    and type(commands[1]) == bytes
                    and len(commands[1]) == 20
                    and commands[2] == 0x87
                ):
                    redeem_script = encode_varstr(command)
                    # we execute the next three op codes
                    commands.pop()
                    h160 = commands.pop()
                    commands.pop()
                    if not op_hash160(stack):
                        return False
                    stack.append(h160)
                    if not op_equal(stack):
                        return False
                    # final result should be a 1
                    if not op_verify(stack):
                        print("bad p2sh h160")
                        return False
                    # hashes match! now add the RedeemScript
                    stream = BytesIO(redeem_script)
                    commands.extend(Script.parse(stream).commands)
                # witness program version 0 rule. if stack commands are:
                # 0 <20 byte hash> this is p2wpkh
                if len(stack) == 2 and stack[0] == b"" and len(stack[1]) == 20:
                    h160 = stack.pop()
                    stack.pop()
                    commands.extend(witness.items)
                    commands.extend(P2PKHScriptPubKey(h160).commands)
                # witness program version 0 rule. if stack commands are:
                # 0 <32 byte hash> this is p2wsh
                if len(stack) == 2 and stack[0] == b"" and len(stack[1]) == 32:
                    s256 = stack.pop()
                    stack.pop()
                    commands.extend(witness.items[:-1])
                    witness_script = witness.items[-1]
                    if s256 != sha256(witness_script):
                        print(
                            "bad sha256 {} vs {}".format(
                                s256.hex(), sha256(witness_script).hex()
                            )
                        )
                        return False
                    # hashes match! now add the Witness Script
                    stream = BytesIO(encode_varstr(witness_script))
                    witness_script_commands = Script.parse(stream).commands
                    commands.extend(witness_script_commands)
        if len(stack) == 0:
            return False
        if stack.pop() == b"":
            return False
        return True

    def is_p2pkh(self):
        """Returns whether the script follows the
        OP_DUP OP_HASH160 <20 byte hash> OP_EQUALVERIFY OP_CHECKSIG pattern."""
        # there should be exactly 5 commands
        # OP_DUP (0x76), OP_HASH160 (0xa9), 20-byte hash, OP_EQUALVERIFY (0x88),
        # OP_CHECKSIG (0xac)
        return (
            len(self.commands) == 5
            and self.commands[0] == 0x76
            and self.commands[1] == 0xA9
            and type(self.commands[2]) == bytes
            and len(self.commands[2]) == 20
            and self.commands[3] == 0x88
            and self.commands[4] == 0xAC
        )

    def is_p2sh(self):
        """Returns whether the script follows the
        OP_HASH160 <20 byte hash> OP_EQUAL pattern."""
        # there should be exactly 3 commands
        # OP_HASH160 (0xa9), 20-byte hash, OP_EQUAL (0x87)
        return (
            len(self.commands) == 3
            and self.commands[0] == 0xA9
            and type(self.commands[1]) == bytes
            and len(self.commands[1]) == 20
            and self.commands[2] == 0x87
        )

    def is_p2wpkh(self):
        """Returns whether the script follows the
        OP_0 <20 byte hash> pattern."""
        return (
            len(self.commands) == 2
            and self.commands[0] == 0x00
            and type(self.commands[1]) == bytes
            and len(self.commands[1]) == 20
        )

    def is_p2wsh(self):
        """Returns whether the script follows the
        OP_0 <32 byte hash> pattern."""
        return (
            len(self.commands) == 2
            and self.commands[0] == 0x00
            and type(self.commands[1]) == bytes
            and len(self.commands[1]) == 32
        )


class ScriptPubKey(Script):
    """Represents a ScriptPubKey in a transaction"""

    @classmethod
    def parse(cls, s):
        script_pubkey = super().parse(s)
        if script_pubkey.is_p2pkh():
            return P2PKHScriptPubKey(script_pubkey.commands[2])
        elif script_pubkey.is_p2sh():
            return P2SHScriptPubKey(script_pubkey.commands[1])
        elif script_pubkey.is_p2wpkh():
            return P2WPKHScriptPubKey(script_pubkey.commands[1])
        else:
            return script_pubkey

    def redeem_script(self):
        """Convert this ScriptPubKey to its RedeemScript equivalent"""
        return RedeemScript(self.commands)


class P2PKHScriptPubKey(ScriptPubKey):
    def __init__(self, h160):
        if type(h160) != bytes:
            raise TypeError("To initialize P2PKHScriptPubKey, a hash160 is needed")
        self.commands = [0x76, 0xA9, h160, 0x88, 0xAC]

    def hash160(self):
        return self.commands[2]

    def address(self, testnet=False):
        if testnet:
            prefix = b"\x6f"
        else:
            prefix = b"\x00"
        # return the encode_base58_checksum the prefix and h160
        return encode_base58_checksum(prefix + self.hash160())


class P2SHScriptPubKey(ScriptPubKey):
    def __init__(self, h160):
        if type(h160) != bytes:
            raise TypeError("To initialize P2SHScriptPubKey, a hash160 is needed")
        self.commands = [0xA9, h160, 0x87]

    def hash160(self):
        return self.commands[1]

    def address(self, testnet=False):
        if testnet:
            prefix = b"\xc4"
        else:
            prefix = b"\x05"
        # return the encode_base58_checksum the prefix and h160
        return encode_base58_checksum(prefix + self.hash160())


class RedeemScript(Script):
    """Subclass that represents a RedeemScript for p2sh"""

    def hash160(self):
        """Returns the hash160 of the serialization of the RedeemScript"""
        return hash160(self.raw_serialize())

    def script_pubkey(self):
        """Returns the ScriptPubKey that this RedeemScript corresponds to"""
        return P2SHScriptPubKey(self.hash160())

    def address(self, testnet=False):
        """Returns the p2sh address for this RedeemScript"""
        return self.script_pubkey().address(testnet)

    @classmethod
    def convert(cls, raw_redeem_script):
        stream = BytesIO(encode_varstr(raw_redeem_script))
        return cls.parse(stream)


class SegwitPubKey(ScriptPubKey):
    def address(self, testnet=False):
        """return the bech32 address for the p2wpkh"""
        # witness program is the raw serialization
        witness_program = self.raw_serialize()
        # convert to bech32 address using encode_bech32_checksum
        return encode_bech32_checksum(witness_program, testnet)

    def p2sh_address(self, testnet=False):
        # get the RedeemScript equivalent and get its address
        return self.redeem_script().address(testnet)


class P2WPKHScriptPubKey(SegwitPubKey):
    def __init__(self, h160):
        if type(h160) != bytes:
            raise TypeError("To initialize P2WPKHScriptPubKey, a hash160 is needed")
        self.commands = [0x00, h160]


class P2WSHScriptPubKey(SegwitPubKey):
    def __init__(self, s256):
        if type(s256) != bytes:
            raise TypeError("To initialize P2WSHScriptPubKey, a sha256 is needed")
        self.commands = [0x00, s256]


class WitnessScript(Script):
    """Subclass that represents a WitnessScript for p2wsh"""

    @classmethod
    def convert(cls, raw_witness_script):
        stream = BytesIO(encode_varstr(raw_witness_script))
        return cls.parse(stream)

    def sha256(self):
        """Returns the sha256 of the raw serialization for witness program"""
        return sha256(self.raw_serialize())

    def script_pubkey(self):
        """Generates the ScriptPubKey for p2wsh"""
        # get the sha256 of the current script
        s256 = self.sha256()
        # return new p2wsh script using p2wsh_script
        return P2WSHScriptPubKey(s256)

    def address(self, testnet=False):
        """Generates a p2wsh address"""
        # grab the entire witness program
        witness_program = self.script_pubkey().raw_serialize()
        # convert to bech32 address using encode_bech32_checksum
        return encode_bech32_checksum(witness_program, testnet)

    def p2sh_address(self, testnet=False):
        """Generates a p2sh-p2wsh address"""
        # the RedeemScript is the p2wsh ScriptPubKey
        redeem_script = self.script_pubkey().redeem_script()
        # return the p2sh address of the RedeemScript (remember testnet)
        return redeem_script.address(testnet)
