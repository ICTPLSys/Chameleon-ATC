#ifndef QEMU_LLFREE_BALLOON_H
#define QEMU_LLFREE_BALLOON_H

#include "exec/cpu-common.h"
#include "qapi/qapi-types-machine.h"

typedef void(QEMULLfreeBalloonResize)(void *opaque, ram_addr_t target, Error **errp);
typedef void(QEMULLfreeBalloonStatus)(void *opaque, LLfreeBalloonInfo *info);
typedef void(QEMULLfreeChameleonConfigure)(void *opaque, ChameleonConfig *config,
                                         Error **errp);

int qemu_add_llfree_balloon_handler(QEMULLfreeBalloonResize *resize_func,
                                    QEMULLfreeBalloonStatus *status_func,
                                    QEMULLfreeChameleonConfigure *configure_func,
                                    void *opaque);
void qemu_remove_llfree_balloon_handler(void *opaque);

#endif
