// SPDX-License-Identifier: GPL-2.0-only
/* Explicitly forget terminal C4 disposable test records after QMP assertions.
 * No page is installed, discarded or released here. Both QUERY and FORGET
 * pass the full immutable identity through the ordinary Guest/Host protocol.
 */
#include <linux/chameleon_transport.h>
#include <linux/debugfs.h>
#include <linux/module.h>
#include <linux/uaccess.h>

static struct dentry *directory;

static ssize_t cleanup_write(struct file *file, const char __user *buffer,
			     size_t count, loff_t *offset)
{
	struct ll_chameleon_range range;
	unsigned long long token, gpa;
	unsigned int pages, order, flags;
	char text[160], extra;
	u32 state;
	int ret;

	if (!count || count >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buffer, count))
		return -EFAULT;
	text[count] = '\0';
	if (sscanf(text, "forget %llu %llu %u %u %u %c", &token, &gpa,
		   &pages, &order, &flags, &extra) != 5 || !token ||
	    order > 9 || order == 1 || pages != (1U << order) ||
	    (gpa & (PAGE_SIZE - 1)) || flags != LL_CH_RANGE_DISCARD_TEST)
		return -EINVAL;
	range = (struct ll_chameleon_range) {
		.token = cpu_to_le64(token), .gpa = cpu_to_le64(gpa),
		.nr_pages = cpu_to_le32(pages), .order = cpu_to_le16(order),
		.flags = cpu_to_le16(flags),
	};
	ret = chameleon_transport_request(LL_CH_OP_QUERY, 0, &range, 1);
	if (ret)
		return ret;
	state = le32_to_cpu(range.state);
	/* QUERY reports the previous operation's status. A valid installed
	 * record can carry EBUSY from the cleanup CANCEL, so inspect state. */
	if (state != LL_CH_STATE_INSTALLED && state != LL_CH_STATE_CANCELED)
		return state == LL_CH_STATE_UNKNOWN ? -ESTALE : -EBUSY;
	range.status = 0;
	range.state = 0;
	ret = chameleon_transport_request(LL_CH_OP_FORGET, 0, &range, 1);
	if (!ret)
		ret = -(int)le32_to_cpu(range.status);
	return ret ? ret : count;
}

static const struct file_operations cleanup_fops = {
	.owner = THIS_MODULE,
	.write = cleanup_write,
};

static int __init cleanup_init(void)
{
	if (!IS_ENABLED(CONFIG_CHAMELEON_TEST) || !chameleon_transport_available())
		return -EOPNOTSUPP;
	directory = debugfs_create_dir("chameleon_control_cleanup", NULL);
	if (IS_ERR(directory))
		return PTR_ERR(directory);
	debugfs_create_file("control", 0200, directory, NULL, &cleanup_fops);
	return 0;
}

static void __exit cleanup_exit(void)
{
	debugfs_remove_recursive(directory);
}

module_init(cleanup_init);
module_exit(cleanup_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Forget exact terminal Chameleon C4 disposable test records");
