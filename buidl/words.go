package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

type (
	Word string
	Words map[Word]int
	Indices map[int]Word
	Mnemonic struct{
		words []Word
		Name string
	}
)

func (m *Mnemonic) String() string {
	ws := make([]string, len(m.words), len(m.words))
	for i, w := range m.words {
		ws[i] = string(w)
	}
	return fmt.Sprintf("Mnemonic{\n  Name: %q,\n  words: %q\n}", m.Name, strings.Join(ws, " "))
}

// NewMnemonic returns a list of mnemonic words chosen from the list of all Words.
func (w *Words) NewMnemonic(mnemonic string) (*Mnemonic, error) {
	parts := strings.Split(mnemonic, " ")
	if len(parts) != 12 {
		return nil, fmt.Errorf("bad number of words: %d", len(parts))
	}
	ws := make([]Word, len(parts), len(parts))
	for i, p := range parts {
		fmt.Printf("[%d] %q: %d\n", i+1, p, w.Index(p))
		ws[i] = Word(p)
	}
	return &Mnemonic{
		words: ws,
		Name: "mnemonic0",
	}, nil
}

func (w *Words) Indices() Indices {
	result := Indices{}
	for k, v := range *w {
		result[v] = k
	}
	return result
}

func (w *Words) Number(n int) Word {
	indices := w.Indices()
	return indices[n]
}

func (w *Words) Index(k string) int {
	return (*w)[Word(k)]
}

func Get() (*Words, error) {
	f, err := os.Open("buidl/words.json")
	if err != nil {
		return nil, err
	}
	defer f.Close()
	d := json.NewDecoder(f)
	ws := make([]Word, 2048, 2048)
	err = d.Decode(&ws)
	if err != nil {
		return nil, err
	}
	result := Words{}
	for i, w := range ws {
		result[w] = i
	}
	return &result, nil
}


func main() {
	words, err := Get()
	if err != nil { panic(err) }
	fmt.Printf("there are %d words\n", len(*words))

	mnemonic := "version keep first say nuclear barely middle castle husband leaf exotic illness"
	mnem, err := words.NewMnemonic(mnemonic)
	if err != nil { panic(err) }
	fmt.Println(mnem.String())
}
