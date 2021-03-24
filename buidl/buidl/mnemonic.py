import inspect
import json

from secrets import randbits

from buidl.helper import int_to_big_endian, sha256


def secure_mnemonic(entropy=0, num_bits=128):
    """Generates a mnemonic phrase using the number of bits"""

    word_list = get_word_list()

    # if we have more than 128 bits, just mask everything but the last 128 bits
    if len(bin(entropy)) > num_bits + 2:
        entropy &= (1 << num_bits) - 1
    # xor some random bits with the entropy that was passed in
    preseed = randbits(num_bits) ^ entropy
    # convert the number to big-endian
    s = int_to_big_endian(preseed, 16)
    # 1 extra bit for checksum is needed per 32 bits
    checksum_bits_needed = num_bits // 32
    # the checksum is the sha256's first n bits. At most this is 8
    checksum = sha256(s)[0] >> (8 - checksum_bits_needed)
    # we concatenate the checksum to the preseed
    total = (preseed << checksum_bits_needed) | checksum
    # now we get the mnemonic passphrase
    mnemonic = []
    # now group into groups of 11 bits
    for _ in range((num_bits + checksum_bits_needed) // 11):
        # grab the last 11 bits
        current = total & ((1 << 11) - 1)
        # insert the correct word at the front
        mnemonic.insert(0, word_list[current])
        # shift by 11 bits so we can move to the next set
        total >>= 11
    # return the mnemonic phrase by putting spaces between
    return " ".join(mnemonic)


def get_word_lookup(word_list):
    lookup = {}
    # go through every word
    for i, word in enumerate(word_list):
        # add the word's index in the hash WORD_LOOKUP
        lookup[word] = i
        # if the word is more than 4 characters, also keep
        #  a lookup of just the first 4 characters
        if len(word) > 4:
            lookup[word[:4]] = i
    return lookup


# map from word as well as word[:3] to index of word
# xx: memoize
def get_word_list():
    cf = inspect.getfile(inspect.currentframe())
    cwd = cf[:-len('mnemonic.py')]
    with open(cwd + "words.json") as wf:
        return json.loads(wf.read())


