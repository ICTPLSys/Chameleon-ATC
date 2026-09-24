// SPDX-License-Identifier: GPL-2.0
/* Guest-side allocation/data-integrity regression. Run only in the test VM. */
#include <linux/module.h>
#include <linux/mm.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/slab.h>
#include <linux/delay.h>

static DEFINE_MUTEX(test_lock);
static char last_result[160] = "NOT_RUN\n";
static unsigned long runs;

static unsigned long expected_word(unsigned long index, unsigned int order,
                                  unsigned long iteration)
{
    return (index * 0x9e3779b97f4a7c15UL) ^ (0xd1b54a32d192ed03UL +
           ((unsigned long)order << 40) + iteration);
}

static int exercise(unsigned int order, unsigned int loops)
{
    unsigned int iteration;
    unsigned long count = (PAGE_SIZE << order) / sizeof(unsigned long);
    for (iteration = 0; iteration < loops; iteration++) {
        struct page *page = alloc_pages(GFP_KERNEL | __GFP_NOWARN, order);
        unsigned long *words, index;
        if (!page)
            return -ENOMEM;
        words = page_address(page);
        if (page_to_pfn(page) & ((1UL << order) - 1)) {
            __free_pages(page, order);
            return -EINVAL;
        }
        for (index = 0; index < count; index++)
            WRITE_ONCE(words[index], expected_word(index, order, iteration));
        cond_resched();
        for (index = 0; index < count; index++) {
            if (READ_ONCE(words[index]) != expected_word(index, order, iteration)) {
                __free_pages(page, order);
                return -EIO;
            }
        }
        __free_pages(page, order);
        cond_resched();
    }
    return 0;
}

/* Hold every block across the verification pass to force new allocator trees. */
static int exercise_batch(unsigned int order, unsigned int blocks)
{
    struct page **pages = kcalloc(blocks, sizeof(*pages), GFP_KERNEL);
    unsigned long count = (PAGE_SIZE << order) / sizeof(unsigned long);
    unsigned int block;
    int error = 0;

    if (!pages)
        return -ENOMEM;
    for (block = 0; block < blocks; block++) {
        unsigned long *words, index;
        pages[block] = alloc_pages(GFP_KERNEL | __GFP_NOWARN | __GFP_NORETRY,
                                  order);
        if (!pages[block]) {
            error = -ENOMEM;
            goto out;
        }
        if (page_to_pfn(pages[block]) & ((1UL << order) - 1)) {
            error = -EINVAL;
            goto out;
        }
        words = page_address(pages[block]);
        for (index = 0; index < count; index++)
            WRITE_ONCE(words[index], expected_word(index, order, block));
        cond_resched();
    }
    for (block = 0; block < blocks; block++) {
        unsigned long *words = page_address(pages[block]), index;
        for (index = 0; index < count; index++) {
            if (READ_ONCE(words[index]) != expected_word(index, order, block)) {
                error = -EIO;
                goto out;
            }
        }
        cond_resched();
    }
out:
    for (block = 0; block < blocks; block++) {
        if (pages[block])
            __free_pages(pages[block], order);
        cond_resched();
    }
    kfree(pages);
    return error;
}

static ssize_t run_write(struct file *file, const char __user *buffer,
                         size_t length, loff_t *offset)
{
    char input[80];
    unsigned int first, last, loops, order, blocks;
    int error = 0;
    if (!length || length >= sizeof(input))
        return -EINVAL;
    if (copy_from_user(input, buffer, length))
        return -EFAULT;
    input[length] = 0;
    if (sscanf(input, "batch %u %u", &order, &blocks) == 2) {
        if (order > 10 || !blocks || blocks > 8192 ||
            ((unsigned long)blocks << order) > (1UL << 20))
            return -EINVAL;
        mutex_lock(&test_lock);
        runs++;
        error = exercise_batch(order, blocks);
        snprintf(last_result, sizeof(last_result),
                 "%s batch run=%lu order=%u blocks=%u held_pages=%lu error=%d\n",
                 error ? "FAIL" : "PASS", runs, order, blocks,
                 (unsigned long)blocks << order, error);
        pr_info("hyperalloc_test: %s", last_result);
        mutex_unlock(&test_lock);
        return error ? error : length;
    }
    if (sscanf(input, "%u %u %u", &first, &last, &loops) != 3 ||
        first > last || last > 10 || !loops || loops > 10000)
        return -EINVAL;
    mutex_lock(&test_lock);
    runs++;
    for (order = first; order <= last; order++) {
        error = exercise(order, loops);
        if (error)
            break;
    }
    snprintf(last_result, sizeof(last_result),
             "%s run=%lu first=%u last=%u loops=%u stopped_order=%u error=%d\n",
             error ? "FAIL" : "PASS", runs, first, last, loops, order, error);
    pr_info("hyperalloc_test: %s", last_result);
    mutex_unlock(&test_lock);
    return error ? error : length;
}

static int result_show(struct seq_file *seq, void *unused)
{
    mutex_lock(&test_lock);
    seq_puts(seq, last_result);
    mutex_unlock(&test_lock);
    return 0;
}

static int result_open(struct inode *inode, struct file *file)
{
    return single_open(file, result_show, NULL);
}

static const struct proc_ops test_ops = {
    .proc_open = result_open,
    .proc_read = seq_read,
    .proc_write = run_write,
    .proc_lseek = seq_lseek,
    .proc_release = single_release,
};

static int __init test_init(void)
{
    return proc_create("hyperalloc_test", 0600, NULL, &test_ops) ? 0 : -ENOMEM;
}

static void __exit test_exit(void)
{
    remove_proc_entry("hyperalloc_test", NULL);
}

module_init(test_init);
module_exit(test_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("HyperAlloc 6.18 guest allocation and data integrity tests");
