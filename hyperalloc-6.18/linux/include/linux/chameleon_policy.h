/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _LINUX_CHAMELEON_POLICY_H
#define _LINUX_CHAMELEON_POLICY_H

/* The policy is runtime-disabled by default. Its hrtimer only queues work;
 * native PSI sampling, MM operations and virtio controls run in that worker.
 * Configuration/target changes require both the timer and lease to be off.
 */

#endif
