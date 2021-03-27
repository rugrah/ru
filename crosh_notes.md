# ctrl+alt+T opens crosh, chromeos dev shell

# see system logs
dmesg -HL

# list vms
vmc list

# grow vm disk without restart
vmc resize termina <larger amount>

# create backup of termina
vmc export termina backup0.tar.gz
