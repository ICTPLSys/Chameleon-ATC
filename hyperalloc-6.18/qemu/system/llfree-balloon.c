/*
 * Generic Balloon handlers and management
 *
 * Copyright (c) 2003-2008 Fabrice Bellard
 * Copyright (C) 2011 Red Hat, Inc.
 * Copyright (C) 2011 Amit Shah <amit.shah@redhat.com>
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */

#include "qemu/osdep.h"
#include "qemu/atomic.h"
#include "sysemu/kvm.h"
#include "sysemu/llfree-balloon.h"
#include "qapi/error.h"
#include "qapi/qapi-commands-machine.h"
#include "qapi/qmp/qerror.h"
#include "trace.h"

static QEMULLfreeBalloonResize *ll_balloon_resize_fn;
static QEMULLfreeBalloonStatus *ll_balloon_status_fn;
static QEMULLfreeChameleonConfigure *ll_chameleon_configure_fn;
static void *ll_balloon_opaque;

static bool have_llfree_balloon(Error **errp)
{
    if (kvm_enabled() && !kvm_has_sync_mmu()) {
        error_set(
            errp, ERROR_CLASS_KVM_MISSING_CAP,
            "Using KVM without synchronous MMU, llfree balloon unavailable");
        return false;
    }
    if (!ll_balloon_resize_fn) {
        error_set(errp, ERROR_CLASS_DEVICE_NOT_ACTIVE,
                  "No llfree balloon device has been activated");
        return false;
    }
    return true;
}

int qemu_add_llfree_balloon_handler(QEMULLfreeBalloonResize *resize_func,
                                    QEMULLfreeBalloonStatus *status_func,
                                    QEMULLfreeChameleonConfigure *configure_func,
                                    void *opaque)
{
    if (ll_balloon_resize_fn || ll_balloon_status_fn || ll_balloon_opaque) {
        /* We're already registered one balloon handler.  How many can
         * a guest really have?
         */
        return -1;
    }
    ll_balloon_resize_fn = resize_func;
    ll_balloon_status_fn = status_func;
    ll_chameleon_configure_fn = configure_func;
    ll_balloon_opaque = opaque;
    return 0;
}

void qemu_remove_llfree_balloon_handler(void *opaque)
{
    if (ll_balloon_opaque != opaque) {
        return;
    }
    ll_balloon_resize_fn = NULL;
    ll_balloon_status_fn = NULL;
    ll_chameleon_configure_fn = NULL;
    ll_balloon_opaque = NULL;
}

LLfreeBalloonInfo *qmp_query_llfree_balloon(Error **errp)
{
    LLfreeBalloonInfo *info;

    if (!have_llfree_balloon(errp)) {
        return NULL;
    }

    info = g_malloc0(sizeof(*info));
    ll_balloon_status_fn(ll_balloon_opaque, info);
    return info;
}

void qmp_chameleon_configure(ChameleonConfig *config, Error **errp)
{
    if (have_llfree_balloon(errp)) {
        ll_chameleon_configure_fn(ll_balloon_opaque, config, errp);
    }
}

void qmp_llfree_balloon(int64_t target, Error **errp)
{
    if (!have_llfree_balloon(errp)) {
        return;
    }

    if (target <= 0) {
        error_setg(errp, QERR_INVALID_PARAMETER_VALUE, "target", "a size");
        return;
    }

    trace_llfree_balloon_event(ll_balloon_opaque, target);
    ll_balloon_resize_fn(ll_balloon_opaque, target, errp);
}
