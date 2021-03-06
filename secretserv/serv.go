// serv checks a directory of secrets
//
// when a change occurs to a file within the directory, the new file is encrypted
// the directories are: secret/ and crypt/
//
// while running, crypt/ is kept up-to-date, with crypt/digest.json recording each
// checksum, and crypt/.lock records the fact that serv is running
package main

import (
	"fmt"
	crypto_rand "crypto/rand"
	"io"
	"io/ioutil"

	"golang.org/x/crypto/nacl/box"
	"github.com/rugrah/ru/secretary"
)

type (
	key *[32]byte
	keyPair struct{
		pub key
		prv key
	}
)

// generateSrvKeys generates the server's persistent keypair
func generateSrvKeys() error {
	pub, prv, err := box.GenerateKey(crypto_rand.Reader)
	if err != nil {	return err }

	b := make([]byte, 32, 32)
	copy(b[:], prv[:])
	err = ioutil.WriteFile("secret/serv_prv.asc", b, 0400)
	if err != nil { return err }
	fmt.Printf("generated serv_prv.asc: %x\n", prv)

	copy(b[:], pub[:])
	err = ioutil.WriteFile("secret/serv_pub.asc", b, 0400)
	if err != nil { return err }
	fmt.Printf("generated serv_pub.asc: %x\n", pub)
	return nil
}

// readSrvKeys reads the server's keys from disk
func readSrvKeys() (*keyPair, error) {
	b, err := ioutil.ReadFile("secret/serv_pub.asc")
	if err != nil { return nil, err }
	if len(b) != 32 {
		return nil, fmt.Errorf("bad length of pub key %d", len(b))
	}
	pub := [32]byte{}
	copy(pub[:], b[:])
	fmt.Printf("read serv_pub.asc: %x\n", pub)

	b, err = ioutil.ReadFile("secret/serv_prv.asc")
	if err != nil { return nil, err }
	if len(b) != 32 {
		return nil, fmt.Errorf("bad length of prv key %d", len(b))
	}
	prv := [32]byte{}
	copy(prv[:], b[:])
	fmt.Printf("read serv_prv.asc: %x\n", prv)

	return &keyPair{pub: &pub, prv: &prv}, nil
}

func main() {
	fmt.Printf("serv starting %q..\n", secretary.Hello("foo.asc"))

	// panic(generateSrvKeys())

	// read the server's keys from disk, these are used as the sender for all AEAD encryption
	//
	// the recipient keys are unique per-file, generated by salsa XOR'ing together sha256 sum
	// of file with the shared passphrase for all files
	//
	// this (sender, receiver) keypairs produces chunks of AEAD data, to be stored in files named
	// after checksum of each chunk, with metadata stored in secret/ and recovered same way as
	// server keys
	srvKeys, err := readSrvKeys()
	if err != nil { panic(err) }

	recipientPublicKey, recipientPrivateKey, err := box.GenerateKey(crypto_rand.Reader)
	if err != nil {
		panic(err)
	}

	// we must use a different nonce for each message you encrypt with the
	// same key
	//
	// since the nonce here is 192 bits long, a random value
	// provides a sufficiently small probability of repeats
	var nonce [24]byte
	if _, err := io.ReadFull(crypto_rand.Reader, nonce[:]); err != nil {
		panic(err)
	}

	msg := []byte("Alas, poor Yorick! I knew him, Horatio")
	// encrypt msg and append result to nonce
	encrypted := box.Seal(nonce[:], msg, &nonce, recipientPublicKey, srvKeys.prv)

	// recipient can decrypt message using their private key and the
	// sender's public key
	//
	// to decrypt, we must use same nonce we used to encrypt message
	//
	// one way to achieve this is to store nonce alongside encrypted message
	var decryptNonce [24]byte
	copy(decryptNonce[:], encrypted[:24])
	decrypted, ok := box.Open(nil, encrypted[24:], &decryptNonce, srvKeys.pub, recipientPrivateKey)
	if !ok {
		panic("decryption error")
	}
	fmt.Println(string(decrypted))
}

