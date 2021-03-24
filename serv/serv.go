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
	"github.com/rugrah/ru/secretary"
)

func main() {
	fmt.Printf("serv starting %q..\n", secretary.Hello("foo.asc"))
}
