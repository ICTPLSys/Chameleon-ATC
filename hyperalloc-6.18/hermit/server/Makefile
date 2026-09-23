CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -Wall -Wextra -Werror
LDLIBS += -lrdmacm -libverbs
.PHONY: all clean
all: rswap-server
rswap-server: rswap_server.c ../wire.h
	$(CC) $(CFLAGS) -o $@ rswap_server.c $(LDLIBS)
clean:
	rm -f rswap-server
