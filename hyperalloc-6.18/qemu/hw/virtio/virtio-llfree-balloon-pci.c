#include "qemu/osdep.h"

#include "hw/virtio/virtio-pci.h"
#include "hw/qdev-properties.h"
#include "hw/virtio/virtio-llfree-balloon.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qom/object.h"

typedef struct VirtIOLLFreeBalloonPCI VirtIOLLFreeBalloonPCI;

/*
 * virtio-llfree-balloon-pci: This extends VirtioPCIProxy.
 */
#define TYPE_VIRTIO_LLFREE_BALLOON_PCI "virtio-llfree-balloon-pci-base"
DECLARE_INSTANCE_CHECKER(VirtIOLLFreeBalloonPCI, VIRTIO_LLFREE_BALLOON_PCI,
			 TYPE_VIRTIO_LLFREE_BALLOON_PCI)

struct VirtIOLLFreeBalloonPCI {
	VirtIOPCIProxy parent_obj;
	VirtIOLLFreeBalloon vdev;
};

// we need to explicitly decide how virtio notifications / kicks should be implemented
// either via ioeventfd (true) => virtqueue will run in main event loop concurrently to vcpu
// or synchronously in vcpu thread (false)
// this has strong implications for the auto deflate mechanism and decides wheter we are single
// or multi-threaded!
static Property virtio_llfree_balloon_properties[] = {
	DEFINE_PROP_BIT("ioeventfd", VirtIOPCIProxy, flags, VIRTIO_PCI_FLAG_USE_IOEVENTFD_BIT,
			true),
	DEFINE_PROP_END_OF_LIST(),
};

static void virtio_llfree_balloon_pci_realize(VirtIOPCIProxy *vpci_dev, Error **errp)
{
	VirtIOLLFreeBalloonPCI *dev = VIRTIO_LLFREE_BALLOON_PCI(vpci_dev);
	DeviceState *vdev = DEVICE(&dev->vdev);

	vpci_dev->class_code = PCI_CLASS_OTHERS;
	qdev_realize(vdev, BUS(&vpci_dev->bus), errp);
}

static void virtio_llfree_balloon_pci_class_init(ObjectClass *klass, void *data)
{
	DeviceClass *dc = DEVICE_CLASS(klass);
	VirtioPCIClass *k = VIRTIO_PCI_CLASS(klass);
	PCIDeviceClass *pcidev_k = PCI_DEVICE_CLASS(klass);
	k->realize = virtio_llfree_balloon_pci_realize;
	set_bit(DEVICE_CATEGORY_MISC, dc->categories);
	pcidev_k->vendor_id = PCI_VENDOR_ID_REDHAT_QUMRANET;
	pcidev_k->device_id = PCI_DEVICE_ID_VIRTIO_BALLOON;
	pcidev_k->revision = VIRTIO_PCI_ABI_VERSION;
	pcidev_k->class_id = PCI_CLASS_OTHERS;
	device_class_set_props(dc, virtio_llfree_balloon_properties);
}

static void virtio_llfree_balloon_pci_instance_init(Object *obj)
{
	VirtIOLLFreeBalloonPCI *dev = VIRTIO_LLFREE_BALLOON_PCI(obj);

	virtio_instance_init_common(obj, &dev->vdev, sizeof(dev->vdev), TYPE_VIRTIO_LLFREE_BALLOON);
}

static const VirtioPCIDeviceTypeInfo virtio_llfree_balloon_pci_info = {
	.base_name = TYPE_VIRTIO_LLFREE_BALLOON_PCI,
	.generic_name = "virtio-llfree-balloon-pci",
	.transitional_name = "virtio-llfree-balloon-pci-transitional",
	.non_transitional_name = "virtio-llfree-balloon-pci-non-transitional",
	.instance_size = sizeof(VirtIOLLFreeBalloonPCI),
	.instance_init = virtio_llfree_balloon_pci_instance_init,
	.class_init = virtio_llfree_balloon_pci_class_init,
};

static void virtio_llfree_balloon_pci_register(void)
{
	virtio_pci_types_register(&virtio_llfree_balloon_pci_info);
}

type_init(virtio_llfree_balloon_pci_register)
