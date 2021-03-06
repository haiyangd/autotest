"""
cgroup autotest test (on KVM guest)
@author: Lukas Doktor <ldoktor@redhat.com>
@copyright: 2011 Red Hat, Inc.
"""
import logging, os, re, sys, tempfile, time
from random import random
from autotest_lib.client.common_lib import error
from autotest_lib.client.bin import utils
from autotest_lib.client.tests.cgroup.cgroup_common import Cgroup, CgroupModules
from autotest_lib.client.tests.cgroup.cgroup_common import get_load_per_cpu
from autotest_lib.client.virt.aexpect import ExpectTimeoutError
from autotest_lib.client.virt.aexpect import ExpectProcessTerminatedError


def run_cgroup(test, params, env):
    """
    Tests the cgroup functions on KVM guests.
     * Uses variable tests (marked by TODO comment) to map the subtests
    """
    vms = None
    tests = None


    # Func
    def _check_vms(vms):
        """
        Checks if the VM is alive.
        @param vms: list of vm's
        """
        err = ""
        for i in range(len(vms)):
            try:
                vms[i].verify_alive()
                vms[i].verify_kernel_crash()
                vms[i].wait_for_login(timeout=30).close()
            except Exception, failure_detail:
                logging.error("_check_vms: %s", failure_detail)
                logging.warn("recreate VM(%s)", i)
                # The vm has to be recreated to reset the qemu PCI state
                vms[i].create()
                err += "%s, " % vms[i].name
        if err:
            raise error.TestFail("WM [%s] had to be recreated" % err[:-2])


    def assign_vm_into_cgroup(vm, cgroup, pwd=None):
        """
        Assigns all threads of VM into cgroup
        @param vm: desired VM
        @param cgroup: cgroup handler
        @param pwd: desired cgroup's pwd, cgroup index or None for root cgroup
        """
        cgroup.set_cgroup(vm.get_shell_pid(), pwd)
        for pid in utils.get_children_pids(vm.get_shell_pid()):
            cgroup.set_cgroup(int(pid), pwd)



    def distance(actual, reference):
        """
        Absolute value of relative distance of two numbers
        @param actual: actual value
        @param reference: reference value
        @return: relative distance abs((a-r)/r) (float)
        """
        return abs(float(actual-reference) / reference)


    def get_dd_cmd(direction, dev=None, count=None, bs=None):
        """
        Generates dd_cmd string
        @param direction: {read,write,bi} dd direction
        @param dev: used device ('vd?')
        @param count: count parameter of dd
        @param bs: bs parameter of dd
        @return: dd command string
        """
        if dev is None:
            if get_device_driver() == "virtio":
                dev = 'vd?'
            else:
                dev = '[sh]d?'
        if direction == "read":
            params = "if=$FILE of=/dev/null iflag=direct"
        elif direction == "write":
            params = "if=/dev/zero of=$FILE oflag=direct"
        else:
            params = "if=$FILE of=$FILE iflag=direct oflag=direct"
        if bs:
            params += " bs=%s" % (bs)
        if count:
            params += " count=%s" % (count)
        return ("export FILE=$(ls /dev/%s | tail -n 1); touch /tmp/cgroup_lock "
                "; while [ -e /tmp/cgroup_lock ]; do dd %s ; done"
                % (dev, params))


    def get_device_driver():
        """
        Discovers the used block device driver {ide, scsi, virtio_blk}
        @return: Used block device driver {ide, scsi, virtio}
        """
        return params.get('drive_format', 'virtio')


    def get_maj_min(dev):
        """
        Returns the major and minor numbers of the dev device
        @return: Tuple(major, minor) numbers of the dev device
        """
        try:
            rdev = os.stat(dev).st_rdev
            ret = (os.major(rdev), os.minor(rdev))
        except Exception, details:
            raise error.TestFail("get_maj_min(%s) failed: %s" %
                                  (dev, details))
        return ret


    def add_file_drive(vm, driver=get_device_driver(), host_file=None):
        """
        Hot-add a drive based on file to a vm
        @param vm: Desired VM
        @param driver: which driver should be used (default: same as in test)
        @param host_file: Which file on host is the image (default: create new)
        @return: Tuple(ret_file, device)
                    ret_file: created file handler (None if not created)
                    device: PCI id of the virtual disk
        """
        # TODO: Implement also via QMP
        if not host_file:
            host_file = tempfile.NamedTemporaryFile(prefix="cgroup-disk-",
                                               suffix=".iso")
            utils.system("dd if=/dev/zero of=%s bs=1M count=8 &>/dev/null"
                         % (host_file.name))
            ret_file = host_file
            logging.debug("add_file_drive: new file %s as drive",
                          host_file.name)
        else:
            ret_file = None
            logging.debug("add_file_drive: using file %s as drive",
                          host_file.name)

        out = vm.monitor.cmd("pci_add auto storage file=%s,if=%s,snapshot=off,"
                             "cache=off" % (host_file.name, driver))
        dev = re.search(r'OK domain (\d+), bus (\d+), slot (\d+), function \d+',
                        out)
        if not dev:
            raise error.TestFail("Can't add device(%s, %s, %s): %s" % (vm.name,
                                                host_file.name, driver, out))
        device = "%02x:%02x" % (int(dev.group(2)), int(dev.group(3)))
        time.sleep(3)
        out = vm.monitor.info('qtree', debug=False)
        if out.count('addr %s.0' % device) != 1:
            raise error.TestFail("Can't add device(%s, %s, %s): device in qtree"
                            ":\n%s" % (vm.name, host_file.name, driver, out))
        return (ret_file, device)


    def add_scsi_drive(vm, driver=get_device_driver(), host_file=None):
        """
        Hot-add a drive based on scsi_debug device to a vm
        @param vm: Desired VM
        @param driver: which driver should be used (default: same as in test)
        @param host_file: Which dev on host is the image (default: create new)
        @return: Tuple(ret_file, device)
                    ret_file: string of the created dev (None if not created)
                    device: PCI id of the virtual disk
        """
        # TODO: Implement also via QMP
        if not host_file:
            if utils.system("lsmod | grep scsi_debug", ignore_status=True):
                utils.system("modprobe scsi_debug dev_size_mb=8 add_host=0")
            utils.system("echo 1 > /sys/bus/pseudo/drivers/scsi_debug/add_host")
            time.sleep(1)   # Wait for device init
            host_file = utils.system_output("ls /dev/sd* | tail -n 1")
            # Enable idling in scsi_debug drive
            utils.system("echo 1 > /sys/block/%s/queue/rotational"
                         % (host_file.split('/')[-1]))
            ret_file = host_file
            logging.debug("add_scsi_drive: add %s device", host_file)
        else:
            # Don't remove this device during cleanup
            # Reenable idling in scsi_debug drive (in case it's not)
            utils.system("echo 1 > /sys/block/%s/queue/rotational"
                         % (host_file.split('/')[-1]))
            ret_file = None
            logging.debug("add_scsi_drive: using %s device", host_file)

        out = vm.monitor.cmd("pci_add auto storage file=%s,if=%s,snapshot=off,"
                             "cache=off" % (host_file, driver))
        dev = re.search(r'OK domain (\d+), bus (\d+), slot (\d+), function \d+',
                        out)
        if not dev:
            raise error.TestFail("Can't add device(%s, %s, %s): %s" % (vm.name,
                                                        host_file, driver, out))
        device = "%02x:%02x" % (int(dev.group(2)), int(dev.group(3)))
        time.sleep(3)
        out = vm.monitor.info('qtree', debug=False)
        if out.count('addr %s.0' % device) != 1:
            raise error.TestFail("Can't add device(%s, %s, %s): device in qtree"
                            ":\n%s" % (vm.name, host_file.name, driver, out))
        return (ret_file, device)


    def rm_drive(vm, host_file, device):
        """
        Remove drive from vm and device on disk
        ! beware to remove scsi devices in reverse order !
        """
        err = False
        # TODO: Implement also via QMP
        if device:
            vm.monitor.cmd("pci_del %s" % device)
            time.sleep(3)
            qtree = vm.monitor.info('qtree', debug=False)
            if qtree.count('addr %s.0' % device) != 0:
                err = True
                vm.destroy()

        if host_file is None:   # Do not remove
            pass
        elif isinstance(host_file, str):    # scsi device
            utils.system("echo -1> /sys/bus/pseudo/drivers/scsi_debug/add_host")
        else:     # file
            host_file.close()

        if err:
            logging.error("Cant del device(%s, %s, %s):\n%s", vm.name,
                                                    host_file, device, qtree)


    # Tests
    class _TestBlkioBandwidth(object):
        """
        BlkioBandwidth dummy test
         * Use it as a base class to an actual test!
         * self.dd_cmd and attr '_set_properties' have to be implemented
         * It prepares 2 vms and run self.dd_cmd to simultaneously stress the
            machines. After 1 minute it kills the dd and gathers the throughput
            information.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vms = vms      # Virt machines
            self.modules = modules          # cgroup module handler
            self.blkio = Cgroup('blkio', '')    # cgroup blkio handler
            self.files = []     # Temporary files (files of virt disks)
            self.devices = []   # Temporary virt devices (PCI drive 1 per vm)
            self.dd_cmd = None  # DD command used to test the throughput

        def cleanup(self):
            """
            Cleanup
            """
            err = ""
            try:
                for i in range(1, -1, -1):
                    rm_drive(vms[i], self.files[i], self.devices[i])
            except Exception, failure_detail:
                err += "\nCan't remove PCI drive: %s" % failure_detail
            try:
                del(self.blkio)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s"
                                      % err)

        def init(self):
            """
            Initialization
             * assigns vm1 and vm2 into cgroups and sets the properties
             * creates a new virtio device and adds it into vms
            """
            if get_device_driver() != 'virtio':
                logging.warn("The main disk for this VM is non-virtio, keep in "
                             "mind that this particular subtest will add a new "
                             "virtio_blk disk to it")
            if self.dd_cmd is None:
                raise error.TestError("Corrupt class, aren't you trying to run "
                                      "parent _TestBlkioBandwidth() function?")
            if len(self.vms) < 2:
                raise error.TestError("Test needs at least 2 vms.")

            # cgroups
            pwd = []
            blkio = self.blkio
            blkio.initialize(self.modules)
            for i in range(2):
                pwd.append(blkio.mk_cgroup())
                assign_vm_into_cgroup(self.vms[i], blkio, pwd[i])
            self.blkio.set_property("blkio.weight", 100, pwd[0])
            self.blkio.set_property("blkio.weight", 1000, pwd[1])

            for i in range(2):
                (host_file, device) = add_file_drive(vms[i], "virtio")
                self.files.append(host_file)
                self.devices.append(device)

        def run(self):
            """
            Actual test:
             * executes self.dd_cmd in a loop simultaneously on both vms and
               gather the throughputs. After 1m finish and calculate the avg.
            """
            sessions = []
            out = []
            sessions.append(vms[0].wait_for_login(timeout=30))
            sessions.append(vms[1].wait_for_login(timeout=30))
            sessions.append(vms[0].wait_for_login(timeout=30))
            sessions.append(vms[1].wait_for_login(timeout=30))
            sessions[0].sendline(self.dd_cmd)
            sessions[1].sendline(self.dd_cmd)
            time.sleep(60)

            # Stop the dd loop and kill all remaining dds
            cmd = "rm -f /tmp/cgroup_lock; killall -9 dd"
            sessions[2].sendline(cmd)
            sessions[3].sendline(cmd)
            re_dd = (r'(\d+) bytes \(\d+\.*\d* \w*\) copied, (\d+\.*\d*) s, '
                      '\d+\.*\d* \w./s')
            out = []
            for i in range(2):
                out.append(sessions[i].read_up_to_prompt())
                out[i] = [int(_[0])/float(_[1])
                            for _ in re.findall(re_dd, out[i])[1:-1]]
                logging.debug("dd(%d) output: %s", i, out[i])
                out[i] = [min(out[i]), sum(out[i])/len(out[i]), max(out[i]),
                          len(out[i])]

            for session in sessions:
                session.close()

            logging.debug("dd values (min, avg, max, ddloops):\nout1: %s\nout2:"
                          " %s", out[0], out[1])

            out1 = out[0][1]
            out2 = out[1][1]
            # Cgroup are limitting weights of guests 100:1000. On bare mettal it
            # works in virtio_blk we are satisfied with the ratio 1:3.
            if out1 == 0:
                raise error.TestFail("No data transfered: %d:%d (1:10)" %
                                      (out1, out2))
            if out1*3  > out2:
                raise error.TestFail("dd values: %d:%d (1:%.2f), limit 1:3"
                                     ", theoretical: 1:10"
                                     % (out1, out2, out2/out1))
            else:
                logging.info("dd values: %d:%d (1:%.2f)", out1, out2, out2/out1)
            return "dd values: %d:%d (1:%.2f)" % (out1, out2, out2/out1)



    class TestBlkioBandwidthWeigthRead(_TestBlkioBandwidth):
        """
        Tests the blkio.weight capability using simultaneous read on 2 vms
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestBlkioBandwidthWeigthRead, self).__init__(vms, modules)
            # Read from the last vd* in a loop until test removes the
            # /tmp/cgroup_lock file (and kills us)
            self.dd_cmd = get_dd_cmd("read", dev='vd?', bs="100K")


    class TestBlkioBandwidthWeigthWrite(_TestBlkioBandwidth):
        """
        Tests the blkio.weight capability using simultaneous write on 2 vms
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            # Write on the last vd* in a loop until test removes the
            # /tmp/cgroup_lock file (and kills us)
            super(TestBlkioBandwidthWeigthWrite, self).__init__(vms, modules)
            self.dd_cmd = get_dd_cmd("write", dev='vd?', bs="100K")


    class _TestBlkioThrottle(object):
        """
        BlkioThrottle dummy test
         * Use it as a base class to an actual test!
         * self.dd_cmd and throughputs have to be implemented
         * It prepares a vm and runs self.dd_cmd. Always after 1 minute switches
           the cgroup. At the end verifies, that the throughputs matches the
           theoretical values.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]    # Virt machines
            self.modules = modules  # cgroup module handler
            self.cgroup = Cgroup('blkio', '')   # cgroup blkio handler
            self.cgroups = []   # List of cgroups directories
            self.files = None   # Temporary files (files of virt disks)
            self.devices = None # Temporary virt devices (PCI drive 1 per vm)
            self.dd_cmd = None  # DD command used to test the throughput
            self.speeds = None  # cgroup throughput

        def cleanup(self):
            """
            Cleanup
            """
            err = ""
            try:
                rm_drive(self.vm, self.files, self.devices)
            except Exception, failure_detail:
                err += "\nCan't remove PCI drive: %s" % failure_detail
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s"
                                      % err)

        def init(self):
            """
            Initialization
             * creates a new virtio device and adds it into vm
             * creates a cgroup for each throughput
            """
            if (self.dd_cmd is None) or (self.speeds) is None:
                raise error.TestError("Corrupt class, aren't you trying to run "
                                      "parent _TestBlkioThrottle() function?")

            if get_device_driver() == "ide":
                logging.warn("The main disk for this VM is ide wich doesn't "
                             "support hot-plug. Using virtio_blk instead")
                (self.files, self.devices) = add_scsi_drive(self.vm,
                                                            driver="virtio")
            else:
                (self.files, self.devices) = add_scsi_drive(self.vm)
            time.sleep(3)
            dev = get_maj_min(self.files)

            cgroup = self.cgroup
            cgroup.initialize(self.modules)
            for i in range(len(self.speeds)):
                speed = self.speeds[i]
                self.cgroups.append(cgroup.mk_cgroup())
                if speed == 0:  # Disable limit (removes the limit)
                    cgroup.set_property("blkio.throttle.write_bps_device",
                                        "%s:%s %s" % (dev[0], dev[1], speed),
                                        check="")
                    cgroup.set_property("blkio.throttle.read_bps_device",
                                        "%s:%s %s" % (dev[0], dev[1], speed),
                                        check="")
                else:       # Enable limit (input separator ' ', output '\t')
                    cgroup.set_property("blkio.throttle.write_bps_device",
                                        "%s:%s %s" % (dev[0], dev[1], speed),
                                        self.cgroups[i], check="%s:%s\t%s"
                                                    % (dev[0], dev[1], speed))
                    cgroup.set_property("blkio.throttle.read_bps_device",
                                        "%s:%s %s" % (dev[0], dev[1], speed),
                                        self.cgroups[i], check="%s:%s\t%s"
                                                    % (dev[0], dev[1], speed))

        def run(self):
            """
            Actual test:
             * executes self.dd_cmd in vm while limiting it's throughput using
               different cgroups (or in a special case only one). At the end
               it verifies the throughputs.
            """
            out = []
            sessions = []
            sessions.append(self.vm.wait_for_login(timeout=30))
            sessions.append(self.vm.wait_for_login(timeout=30))
            sessions[0].sendline(self.dd_cmd)
            for i in range(len(self.cgroups)):
                logging.info("Limiting speed to: %s", (self.speeds[i]))
                # Assign all threads of vm
                assign_vm_into_cgroup(self.vm, self.cgroup, self.cgroups[i])

                # Standard test-time is 60s. If the slice time is less than 30s,
                # test-time is prolonged to 30s per slice.
                time.sleep(max(60/len(self.speeds), 30))
                sessions[1].sendline("rm -f /tmp/cgroup_lock; killall -9 dd")
                out.append(sessions[0].read_up_to_prompt())
                sessions[0].sendline(self.dd_cmd)
                time.sleep(random()*0.05)

            sessions[1].sendline("rm -f /tmp/cgroup_lock; killall -9 dd")
            # Verification
            re_dd = (r'(\d+) bytes \(\d+\.*\d* \w*\) copied, (\d+\.*\d*) s, '
                      '\d+\.*\d* \w./s')
            err = []
            for i in range(len(out)):
                out[i] = [int(int(_[0])/float(_[1]))
                              for _ in re.findall(re_dd, out[i])]
                if not out[i]:
                    raise error.TestFail("Not enough samples; please increase"
                                         "throughput speed or testing time;"
                                         "\nsamples: %s" % (out[i]))
                # First samples might be corrupted, use only last sample when
                # not enough data. (which are already an avg of 3xBS)
                warn = False
                if len(out[i]) < 3:
                    warn = True
                    out[i] = [out[i][-1]]
                count = len(out[i])
                out[i].sort()
                # out = [min, med, max, number_of_samples]
                out[i] = [out[i][0], out[i][count/2], out[i][-1], count]
                if warn:
                    logging.warn("Not enough samples, using the last one (%s)",
                                 out[i])
                if ((self.speeds[i] != 0) and
                        (distance(out[i][1], self.speeds[i]) > 0.1)):
                    logging.error("The throughput didn't match the requirements"
                                  "(%s !~ %s)", out[i], self.speeds[i])
                    err.append(i)

            if self.speeds.count(0) > 1:
                unlimited = []
                for i in range(len(self.speeds)):
                    if self.speeds[i] == 0:
                        unlimited.append(out[i][1])
                        self.speeds[i] = "(inf)"

                avg = sum(unlimited) / len(unlimited)
                if avg == 0:
                    logging.warn("Average unlimited speed is 0 (%s)", out)
                else:
                    for speed in unlimited:
                        if distance(speed, avg) > 0.1:
                            logging.warning("Unlimited speeds variates during "
                                            "the test: %s", unlimited)
                            break


            out_speeds = ["%s ~ %s" % (out[i][1], self.speeds[i])
                                        for i in range(len(self.speeds))]
            if err:
                if len(out) == 1:
                    raise error.TestFail("Actual throughput: %s, theoretical: "
                                         "%s" % (out[0][1], self.speeds[0]))
                elif len(err) == len(out):
                    raise error.TestFail("All throughput limits were broken "
                                         "(%s)" % (out_speeds))
                else:
                    raise error.TestFail("Limits (%s) were broken (%s)"
                                         % (err, out_speeds))

            return ("All throughputs matched their limits (%s)" % out_speeds)


    class TestBlkioThrottleRead(_TestBlkioThrottle):
        """ Tests the blkio.throttle.read_bps_device """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestBlkioThrottleRead, self).__init__(vms, modules)
            self.dd_cmd = get_dd_cmd("read", count=1)
            self.speeds = [1024]


    class TestBlkioThrottleWrite(_TestBlkioThrottle):
        """ Tests the blkio.throttle.write_bps_device """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestBlkioThrottleWrite, self).__init__(vms, modules)
            self.dd_cmd = get_dd_cmd("write", count=1)
            self.speeds = [1024]


    class TestBlkioThrottleMultipleRead(_TestBlkioThrottle):
        """
        Tests the blkio.throttle.read_bps_device while switching multiple
        cgroups with different speeds.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestBlkioThrottleMultipleRead, self).__init__(vms, modules)
            self.dd_cmd = get_dd_cmd("read", count=1)
            self.speeds = [0, 1024, 0, 2048, 0, 4096]


    class TestBlkioThrottleMultipleWrite(_TestBlkioThrottle):
        """
        Tests the blkio.throttle.write_bps_device while switching multiple
        cgroups with different speeds.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestBlkioThrottleMultipleWrite, self).__init__(vms, modules)
            self.dd_cmd = get_dd_cmd("write", count=1)
            self.speeds = [0, 1024, 0, 2048, 0, 4096]


    class TestDevicesAccess:
        """
        It tries to attach scsi_debug disk with different cgroup devices.list
        setting.
         * self.permissions are defined as a list of dictionaries:
           {'property': control property, 'value': permition value,
            'check_value': check value (from devices.list property),
            'read_results': excepced read results T/F,
            'write_results': expected write results T/F}
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]      # Virt machines
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('devices', '')   # cgroup blkio handler
            self.files = None   # Temporary files (files of virt disks)
            self.devices = None # Temporary virt devices
            self.permissions = None  # Test dictionary, see init for details


        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                rm_drive(self.vm, self.files, self.devices)
            except Exception, failure_detail:
                err += "\nCan't remove PCI drive: %s" % failure_detail
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s"
                                      % err)


        def init(self):
            """
            Initialization
             * creates a new scsi_debug device
             * prepares one cgroup and assign vm to it
            """
            # Only create the host /dev/sd? device
            (self.files, self.devices) = add_scsi_drive(self.vm)
            rm_drive(self.vm, host_file=None, device=self.devices)
            self.devices = None # We don't want to mess cleanup

            time.sleep(3)
            dev = "%s:%s" % get_maj_min(self.files)

            self.cgroup.initialize(self.modules)
            self.cgroup.mk_cgroup()
            assign_vm_into_cgroup(self.vm, self.cgroup, 0)

            # Test dictionary
            # Beware of persistence of some setting to another round!!!
            self.permissions = [
                               {'property'      : 'deny',
                                'value'         : 'a',
                                'check_value'   : '',
                                'result'        : False},
                               {'property'      : 'allow',
                                'value'         : 'b %s rm' % dev,
                                'check_value'   : True,
                                'result'        : False},
                               {'property'      : 'allow',
                                'value'         : 'b %s w' % dev,
                                'check_value'   : 'b %s rwm' % dev,
                                'result'        : True},
                               {'property'      : 'deny',
                                'value'         : 'b %s r' % dev,
                                'check_value'   : 'b %s wm' % dev,
                                'result'        : False},
                               {'property'      : 'deny',
                                'value'         : 'b %s wm' % dev,
                                'check_value'   : '',
                                'result'        : False},
                               {'property'      : 'allow',
                                'value'         : 'a',
                                'check_value'   : 'a *:* rwm',
                                'result'        : True},
                              ]



        def run(self):
            """
            Actual test:
             * For each self.permissions sets the cgroup devices permition
               and tries attach the disk. Checks the results with prescription.
            """
            def set_permissions(cgroup, permissions):
                """
                Wrapper for setting permissions to first cgroup
                @param self.permissions: is defined as a list of dictionaries:
                   {'property': control property, 'value': permition value,
                    'check_value': check value (from devices.list property),
                    'read_results': excepced read results T/F,
                    'write_results': expected write results T/F}
                """
                cgroup.set_property('devices.'+permissions['property'],
                                    permissions['value'],
                                    cgroup.cgroups[0],
                                    check=permissions['check_value'],
                                    checkprop='devices.list')


            session = self.vm.wait_for_login(timeout=30)

            cgroup = self.cgroup
            results = ""
            for perm in self.permissions:
                set_permissions(cgroup, perm)
                logging.debug("Setting permissions: {%s: %s}, value: %s",
                              perm['property'], perm['value'],
                              cgroup.get_property('devices.list',
                                                  cgroup.cgroups[0]))

                try:
                    (_, self.devices) = add_scsi_drive(self.vm,
                                                        host_file=self.files)
                except Exception, details:
                    if perm['result']:
                        logging.error("Perm: {%s: %s}: drive was not attached:"
                                      " %s", perm['property'], perm['value'],
                                      details)
                        results += ("{%s: %s => NotAttached}, " %
                                     (perm['property'], perm['value']))
                else:
                    if not perm['result']:
                        logging.error("Perm: {%s: %s}: drive was attached",
                                      perm['property'], perm['value'])
                        results += ("{%s: %s => Attached}, " %
                                     (perm['property'], perm['value']))
                    rm_drive(self.vm, host_file=None, device=self.devices)
                    self.devices = None

            session.close()
            if results:
                raise error.TestFail("Some restrictions were broken: {%s}" %
                                      results[:-2])

            time.sleep(10)

            return ("All restrictions enforced successfully.")


    class TestFreezer:
        """
        Tests the freezer.state cgroup functionality. (it freezes the guest
        and unfreeze it again)
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]      # Virt machines
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('freezer', '')   # cgroup blkio handler
            self.files = None   # Temporary files (files of virt disks)
            self.devices = None # Temporary virt devices


        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                self.cgroup.set_property('freezer.state', 'THAWED',
                                         self.cgroup.cgroups[0])
            except Exception, failure_detail:
                err += "\nCan't unfreeze vm: %s" % failure_detail

            try:
                _ = self.vm.wait_for_login(timeout=30)
                _.cmd('rm -f /tmp/freeze-lock')
                _.close()
            except Exception, failure_detail:
                err += "\nCan't stop the stresses."

            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestFail("Some cleanup operations failed: %s" %
                                      err)


        def init(self):
            """
            Initialization
             * prepares one cgroup and assign vm to it
            """
            self.cgroup.initialize(self.modules)
            self.cgroup.mk_cgroup()
            assign_vm_into_cgroup(self.vm, self.cgroup, 0)


        def run(self):
            """
            Actual test:
             * Freezes the guest and thaws it again couple of times
             * verifies that guest is frozen and runs when expected
            """
            def _get_stat(pid):
                """
                Gather statistics of pid+1st level subprocesses cpu usage
                @param pid: PID of the desired process
                @return: sum of all cpu-related values of 1st level subprocesses
                """
                out = None
                for i in range(10):
                    try:
                        out = utils.system_output("cat /proc/%s/task/*/stat" %
                                                   pid)
                    except error.CmdError:
                        out = None
                    else:
                        break
                out = out.split('\n')
                ret = 0
                for i in out:
                    ret += sum([int(_) for _ in i.split(' ')[13:17]])
                return ret


            session = self.vm.wait_for_serial_login(timeout=30)
            session.cmd('touch /tmp/freeze-lock')
            session.sendline('while [ -e /tmp/freeze-lock ]; do :; done')
            cgroup = self.cgroup
            pid = self.vm.get_pid()

            for tsttime in [0.5, 3, 20]:
                # Let it work for short, mid and long period of time
                logging.info("FREEZING (%ss)", tsttime)
                # Death line for freezing is 1s
                cgroup.set_property('freezer.state', 'FROZEN',
                                    cgroup.cgroups[0], check=False)
                time.sleep(1)
                _ = cgroup.get_property('freezer.state', cgroup.cgroups[0])
                if 'FROZEN' not in _:
                    raise error.TestFail("Couldn't freeze the VM: state %s" % _)
                stat_ = _get_stat(pid)
                time.sleep(tsttime)
                stat = _get_stat(pid)
                if stat != stat_:
                    raise error.TestFail('Process was running in FROZEN state; '
                                         'stat=%s, stat_=%s, diff=%s' %
                                          (stat, stat_, stat-stat_))
                logging.info("THAWING (%ss)", tsttime)
                self.cgroup.set_property('freezer.state', 'THAWED',
                                         self.cgroup.cgroups[0])
                stat_ = _get_stat(pid)
                time.sleep(tsttime)
                stat = _get_stat(pid)
                if (stat - stat_) < (90*tsttime):
                    raise error.TestFail('Process was not active in FROZEN'
                                         'state; stat=%s, stat_=%s, diff=%s' %
                                          (stat, stat_, stat-stat_))

            return ("Freezer works fine")


    class TestMemoryMove:
        """
        Tests the memory.move_charge_at_immigrate cgroup capability. It changes
        memory cgroup while running the guest system.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]      # Virt machines
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('memory', '')   # cgroup blkio handler

        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s" %
                                       err)


        def init(self):
            """ Initialization: prepares two cgroups """
            self.cgroup.initialize(self.modules)
            self.cgroup.mk_cgroup()
            self.cgroup.mk_cgroup()
            assign_vm_into_cgroup(self.vm, self.cgroup, 0)

            self.cgroup.set_property('memory.move_charge_at_immigrate', '3',
                                     self.cgroup.cgroups[0])
            self.cgroup.set_property('memory.move_charge_at_immigrate', '3',
                                     self.cgroup.cgroups[1])


        def run(self):
            """ Actual test: change cgroup while running test command """
            sessions = []
            sessions.append(self.vm.wait_for_login(timeout=30))
            sessions.append(self.vm.wait_for_login(timeout=30))

            # Don't allow to specify more than 1/2 of the VM's memory
            size = int(params.get('mem', 1024)) / 2
            if params.get('cgroup_memory_move_mb') is not None:
                size = min(size, int(params.get('cgroup_memory_move_mb')))

            sessions[0].sendline('dd if=/dev/zero of=/dev/null bs=%dM '
                                 'iflag=fullblock' % size)
            time.sleep(2)

            sessions[1].cmd('killall -SIGUSR1 dd')
            for i in range(10):
                logging.debug("Moving vm into cgroup %s.", (i%2))
                assign_vm_into_cgroup(self.vm, self.cgroup, i%2)
                time.sleep(0.1)
            time.sleep(2)
            sessions[1].cmd('killall -SIGUSR1 dd')
            try:
                out = sessions[0].read_until_output_matches(
                                                ['(\d+)\+\d records out'])[1]
                if len(re.findall(r'(\d+)\+\d records out', out)) < 2:
                    out += sessions[0].read_until_output_matches(
                                                ['(\d+)\+\d records out'])[1]
            except ExpectTimeoutError:
                raise error.TestFail("dd didn't produce expected output: %s" %
                                      out)

            sessions[1].cmd('killall dd')
            dd_res = re.findall(r'(\d+)\+(\d+) records in', out)
            dd_res += re.findall(r'(\d+)\+(\d+) records out', out)
            dd_res = [int(_[0]) + int(_[1]) for _ in dd_res]
            if dd_res[1] <= dd_res[0] or dd_res[3] <= dd_res[2]:
                raise error.TestFail("dd stoped sending bytes: %s..%s, %s..%s" %
                                      (dd_res[0], dd_res[1], dd_res[2],
                                       dd_res[3]))

            return ("Guest moved 10times while creating %dMB blocks" % size)


    class TestMemoryLimit:
        """ Tests the memory.limit_in_bytes by triyng to break the limit """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]      # Virt machines
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('memory', '')   # cgroup blkio handler


        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s" %
                                       err)


        def init(self):
            """
            Initialization: prepares the cgroup and starts new VM inside it.
            """
            # Don't allow to specify more than 1/2 of the VM's memory
            mem = int(params.get('mem', 1024)) * 512
            if params.get('cgroup_memory_limit_kb') is not None:
                mem = min(mem, int(params.get('cgroup_memory_limit_kb')))

            self.cgroup.initialize(self.modules)
            self.cgroup.mk_cgroup()
            self.cgroup.set_property('memory.move_charge_at_immigrate', '3',
                                     self.cgroup.cgroups[0])
            self.cgroup.set_property_h('memory.limit_in_bytes', "%dK" % mem,
                                     self.cgroup.cgroups[0])

            logging.info("Expected VM reload")
            try:
                self.vm.create()
            except Exception, failure_detail:
                raise error.TestFail("init: Failed to recreate the VM: %s" %
                                      failure_detail)
            assign_vm_into_cgroup(self.vm, self.cgroup, 0)
            timeout = int(params.get("login_timeout", 360))
            self.vm.wait_for_login(timeout=timeout).close()
            status = open('/proc/%s/status' % self.vm.get_pid(), 'r').read()
            rss = int(re.search(r'VmRSS:[\t ]*(\d+) kB', status).group(1))
            if rss > mem:
                raise error.TestFail("Init failed to move VM into cgroup, VmRss"
                                     "=%s, expected=%s" % (rss, mem))

        def run(self):
            """
            Run dd with bs > memory limit. Verify that qemu survives and
            success in executing the command without breaking off the limit.
            """
            session = self.vm.wait_for_login(timeout=30)

            # Use 1.1 * memory_limit block size
            mem = int(params.get('mem', 1024)) * 512
            if params.get('cgroup_memory_limit_kb') is not None:
                mem = min(mem, int(params.get('cgroup_memory_limit_kb')))
            mem *= 1.1
            session.sendline('dd if=/dev/zero of=/dev/null bs=%dK count=1 '
                             'iflag=fullblock' %mem)

            # Check every 0.1s VM memory usage. Limit the maximum execution time
            # to mem / 10 (== mem * 0.1 sleeps)
            max_rss = 0
            max_swap = 0
            out = ""
            for _ in range(int(mem / 1024)):
                status = open('/proc/%s/status' % self.vm.get_pid(), 'r').read()
                rss = int(re.search(r'VmRSS:[\t ]*(\d+) kB', status).group(1))
                max_rss = max(rss, max_rss)
                swap = int(re.search(r'VmSwap:[\t ]*(\d+) kB', status).group(1))
                max_swap = max(swap + rss, max_swap)
                try:
                    out += session.read_up_to_prompt(timeout=0.1)
                except ExpectTimeoutError:
                    #0.1s passed, lets begin the next round
                    pass
                except ExpectProcessTerminatedError, failure_detail:
                    raise error.TestFail("VM failed executing the command: %s" %
                                          failure_detail)
                else:
                    break

            if max_rss > mem:
                raise error.TestFail("The limit was broken: max_rss=%s, limit="
                                     "%s" % (max_rss, mem))
            exit_nr = session.cmd_output("echo $?")[:-1]
            if exit_nr != '0':
                raise error.TestFail("dd command failed: %s, output: %s" %
                                      (exit_nr, out))
            if (max_rss + max_swap) < mem:
                raise error.TestFail("VM didn't consume expected amount of "
                                     "memory. Output of dd cmd: %s" % out)

            return ("Created %dMB block with 1.1 limit overcommit" % (mem/1024))


    class _TestCpuShare(object):
        """
        Tests the cpu.share cgroup capability. It creates n cgroups accordingly
        to self.speeds variable and sufficient VMs to symetricaly test three
        different scenerios.
        1) #threads == #CPUs
        2) #threads + 1 == #CPUs, +1thread have the lowest priority (or equal)
        3) #threads * #cgroups == #CPUs
        Cgroup shouldn't slow down VMs on unoccupied CPUs. With thread
        overcommit the scheduler should stabilize accordingly to speeds
        value.
        """
        def __init__(self, vms, modules):
            self.vms = vms[:]      # Copy of virt machines
            self.vms_count = len(vms) # Original number of vms
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('cpu', '')   # cgroup blkio handler
            self.speeds = None  # cpu.share values [cg1, cg2]
            self.sessions = []    # ssh sessions
            self.serials = []   # serial consoles


        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            # Stop all VMS in parallel, then check for success.
            for i in range(len(self.vms)):
                self.serials[i].sendline('rm -f /tmp/cgroup-cpu-lock')
            time.sleep(2)
            for i in range(len(self.vms)):
                try:
                    out = self.serials[i].cmd_output('echo $?', timeout=10)
                    if out != "0\n":
                        err += ("\nCan't stop the stresser on %s: %s" %
                                self.vms[i].name)
                except Exception, failure_detail:
                    err += ("\nCan't stop the stresser on %s: %s" %
                             (self.vms[i].name, failure_detail))
            del self.serials

            for i in range(len(self.sessions)):
                try:
                    self.sessions[i].close()
                except Exception, failure_detail:
                    err += ("\nCan't close ssh connection %s" % i)
            del self.sessions

            for vm in self.vms[self.vms_count:]:
                try:
                    vm.destroy(gracefully=False)
                except Exception, failure_detail:
                    err += "\nCan't destroy added VM: %s" % failure_detail
            del self.vms

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s"
                                      % err)


        def init(self):
            """
            Initialization
             * creates additional VMs to fit the  no_cgroups * host_cpus /
               vm_cpus requirement (self.vms + additional VMs)
             * creates two cgroups and sets cpu.share accordingly to self.speeds
            """
            self.speeds.sort()
            host_cpus = open('/proc/cpuinfo').read().count('model name')
            vm_cpus = int(params.get('smp', 1)) # cpus per VM
            no_speeds = len(self.speeds)        # #cgroups
            no_vms = host_cpus * no_speeds / vm_cpus    # #VMs used by test
            no_threads = no_vms * vm_cpus       # total #threads
            sessions = self.sessions
            for i in range(no_vms - self.vms_count):    # create needed VMs
                vm_name = "clone%s" % i
                self.vms.append(self.vms[0].clone(vm_name, params))
                env.register_vm(vm_name, self.vms[-1])
                self.vms[-1].create()
            timeout = 1.5 * int(params.get("login_timeout", 360))
            for i in range(no_threads):
                sessions.append(self.vms[i%no_vms].wait_for_login(
                                                            timeout=timeout))
            self.cgroup.initialize(self.modules)
            for i in range(no_speeds):
                self.cgroup.mk_cgroup()
                self.cgroup.set_property('cpu.shares', self.speeds[i], i)
            for i in range(no_vms):
                assign_vm_into_cgroup(self.vms[i], self.cgroup, i%no_speeds)
                sessions[i].cmd("touch /tmp/cgroup-cpu-lock")
                self.serials.append(self.vms[i].wait_for_serial_login(
                                                                timeout=30))


        def run(self):
            """
            Actual test:
            Let each of 3 scenerios (described in test specification) stabilize
            and then measure the CPU utilisation for time_test time.
            """
            def _get_stat(f_stats, _stats=None):
                """ Reads CPU times from f_stats[] files and sumarize them. """
                if _stats is None:
                    _stats = []
                    for i in range(len(f_stats)):
                        _stats.append(0)
                stats = []
                for i in range(len(f_stats)):
                    f_stats[i].seek(0)
                    stats.append(f_stats[i].read().split()[13:17])
                    stats[i] = sum([int(_) for _ in stats[i]]) - _stats[i]
                return stats


            host_cpus = open('/proc/cpuinfo').read().count('model name')
            no_speeds = len(self.speeds)
            no_threads = host_cpus * no_speeds       # total #threads
            sessions = self.sessions
            f_stats = []
            err = []
            for vm in self.vms:
                f_stats.append(open("/proc/%d/stat" % vm.get_pid(), 'r'))

            time_init = 10
            time_test = 10
            thread_count = 0    # actual thread number
            stats = []
            cmd = "renice -n 10 $$; " # new ssh login should pass
            cmd += "while [ -e /tmp/cgroup-cpu-lock ]; do :; done"
            for thread_count in range(0, host_cpus):
                sessions[thread_count].sendline(cmd)
            time.sleep(time_init)
            _stats = _get_stat(f_stats)
            time.sleep(time_test)
            stats.append(_get_stat(f_stats, _stats))

            thread_count += 1
            sessions[thread_count].sendline(cmd)
            if host_cpus % no_speeds == 0 and no_speeds <= host_cpus:
                time.sleep(time_init)
                _stats = _get_stat(f_stats)
                time.sleep(time_test)
                stats.append(_get_stat(f_stats, _stats))

            for i in range(thread_count+1, no_threads):
                sessions[i].sendline(cmd)
            time.sleep(time_init)
            _stats = _get_stat(f_stats)
            for j in range(3):
                __stats = _get_stat(f_stats)
                time.sleep(time_test)
                stats.append(_get_stat(f_stats, __stats))
            stats.append(_get_stat(f_stats, _stats))

            # Verify results
            err = ""
            # accumulate stats from each cgroup
            for j in range(len(stats)):
                for i in range(no_speeds, len(stats[j])):
                    stats[j][i % no_speeds] += stats[j][i]
                stats[j] = stats[j][:no_speeds]
            # I.
            i = 0
            dist = distance(min(stats[i]), max(stats[i]))
            if dist > min(0.10 + 0.01 * len(self.vms), 0.2):
                err += "1, "
                logging.error("1st part's limits broken. Utilisation should be "
                              "equal. stats = %s, distance = %s", stats[i],
                              dist)
            # II.
            i += 1
            if len(stats) == 6:
                dist = distance(min(stats[i]), max(stats[i]))
                if dist > min(0.10 + 0.01 * len(self.vms), 0.2):
                    err += "2, "
                    logging.error("2nd part's limits broken, Utilisation "
                                  "should be equal. stats = %s, distance = %s",
                                  stats[i], dist)

            # III.
            # normalize stats, then they should have equal values
            i += 1
            for i in range(i, len(stats)):
                norm_stats = [float(stats[i][_]) / self.speeds[_]
                                                for _ in range(len(stats[i]))]
                dist = distance(min(norm_stats), max(norm_stats))
                if dist > min(0.10 + 0.02 * len(self.vms), 0.25):
                    err += "3, "
                    logging.error("3rd part's limits broken; utilisation should"
                                  " be in accordance to self.speeds. stats=%s"
                                  ", norm_stats=%s, distance=%s, speeds=%s,it="
                                  "%d", stats[i], norm_stats, dist,
                                  self.speeds, i-1)

            if err:
                err = "[%s] parts broke their limits" % err[:-2]
                logging.error(err)
                raise error.TestFail(err)

            return ("Cpu utilisation enforced succesfully")


    class TestCpuShare10(_TestCpuShare):
        """
        1:10 variant of _TestCpuShare test.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestCpuShare10, self).__init__(vms, modules)
            self.speeds = [10000, 100000]


    class TestCpuShare50(_TestCpuShare):
        """
        1:1 variant of _TestCpuShare test.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            super(TestCpuShare50, self).__init__(vms, modules)
            self.speeds = [100000, 100000]


    class TestCpuCFSUtil:
        """
        Tests the utilisation of scheduler when cgroup cpu.cfs_* setting is
        set. There is a known issue with scheduler and multiple CPUs.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vms = vms[:]      # Copy of virt machines
            self.vms_count = len(vms) # Original number of vms
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('cpu', '')   # cgroup blkio handler
            self.sessions = []    # ssh sessions
            self.serials = []   # serial consoles


        def cleanup(self):
            """ Cleanup """
            err = ""
            del(self.cgroup)

            for i in range(len(self.vms)):
                self.serials[i].sendline('rm -f /tmp/cgroup-cpu-lock')
            del self.serials

            for i in range(len(self.sessions)):
                try:
                    self.sessions[i].close()
                except Exception, failure_detail:
                    err += ("\nCan't close ssh connection %s" % i)
            del self.sessions

            for vm in self.vms[self.vms_count:]:
                try:
                    vm.destroy(gracefully=False)
                except Exception, failure_detail:
                    err += "\nCan't destroy added VM: %s" % failure_detail
            del self.vms

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s"
                                      % err)


        def init(self):
            """
            Initialization
             * creates additional VMs (vm_cpus = 2 * host_cpus)
             * creates cgroup for each VM and subcgroup for theirs vCPUs
               (../vm[123..]/vcpu[012..])
            """
            def get_cpu_pids(vm, smp=None):
                """ Get pids of all VM's vcpus """
                cpu_pids = re.findall(r'thread_id=(\d+)',
                                      vm.monitor.info("cpus"))
                if not cpu_pids:
                    raise error.TestFail("Can't get 'info cpus' from monitor")
                if smp is not None and len(cpu_pids) != smp:
                    raise error.TestFail("Incorrect no vcpus: monitor = %s, "
                                         "params = %s" % (len(cpu_pids), smp))
                return cpu_pids

            self.cgroup.initialize(self.modules)
            host_cpus = open('/proc/cpuinfo').read().count('model name')
            smp = int(params.get('smp', 1))
            vm_cpus = 0
            # Prepare existing vms (if necessarily)
            for i in range(min(len(self.vms), 2 * host_cpus / smp)):
                # create "/vm[123]/ cgroups and set cfs_quota_us to no_vcpus*50%
                vm_pwd = self.cgroup.mk_cgroup()
                self.cgroup.set_property("cpu.cfs_period_us", 100000, vm_pwd)
                self.cgroup.set_property("cpu.cfs_quota_us", 50000*smp, vm_pwd)
                assign_vm_into_cgroup(self.vms[i], self.cgroup, vm_pwd)
                cpu_pids = get_cpu_pids(self.vms[i], smp)
                for j in range(smp):
                    # create "/vm*/vcpu[123] cgroups and set cfs_quota_us to 50%
                    vcpu_pwd = self.cgroup.mk_cgroup(vm_pwd)
                    self.cgroup.set_property("cpu.cfs_period_us", 100000,
                                                                    vcpu_pwd)
                    self.cgroup.set_property("cpu.cfs_quota_us", 50000,
                                                                    vcpu_pwd)
                    self.cgroup.set_cgroup(int(cpu_pids[j]), vcpu_pwd)
                    self.sessions.append(self.vms[i].wait_for_login(timeout=30))
                    vm_cpus += 1
                self.serials.append(self.vms[i].wait_for_serial_login(
                                                                    timeout=30))
                self.serials[-1].cmd("touch /tmp/cgroup-cpu-lock")
            timeout = 1.5 * int(params.get("login_timeout", 360))
            _params = params
            # Add additional vms (if necessarily)
            i = 0
            while vm_cpus < 2 * host_cpus:
                vm_name = "clone%s" % i
                smp = min(vm_cpus, 2 * host_cpus - vm_cpus)
                _params['smp'] = smp
                self.vms.append(self.vms[0].clone(vm_name, _params))
                env.register_vm(vm_name, self.vms[-1])
                self.vms[-1].create()
                vm_pwd = self.cgroup.mk_cgroup()
                self.cgroup.set_property("cpu.cfs_period_us", 100000, vm_pwd)
                self.cgroup.set_property("cpu.cfs_quota_us", 50000*smp, vm_pwd)
                assign_vm_into_cgroup(self.vms[-1], self.cgroup, vm_pwd)
                cpu_pids = get_cpu_pids(self.vms[-1], smp)
                for j in range(smp):
                    vcpu_pwd = self.cgroup.mk_cgroup(vm_pwd)
                    self.cgroup.set_property("cpu.cfs_period_us", 100000,
                                                                    vcpu_pwd)
                    self.cgroup.set_property("cpu.cfs_quota_us", 50000,
                                                                    vcpu_pwd)
                    self.cgroup.set_cgroup(int(cpu_pids[j]), vcpu_pwd)
                    self.sessions.append(self.vms[-1].wait_for_login(
                                                            timeout=timeout))
                self.serials.append(self.vms[-1].wait_for_serial_login(
                                                                    timeout=30))
                self.serials[-1].cmd("touch /tmp/cgroup-cpu-lock")
                vm_cpus += smp
                i += 1


        def run(self):
            """
            Actual test:
            It run stressers on all vcpus, gather host CPU utilisation and
            verifies that guests use at least 95% of CPU time.
            """
            stats = []
            cmd = "renice -n 10 $$; "
            cmd += "while [ -e /tmp/cgroup-cpu-lock ]; do :; done"
            for session in self.sessions:
                session.sendline(cmd)

            # Test
            time.sleep(1)
            stats.append(open('/proc/stat', 'r').readline())
            time.sleep(1)
            stats.append(open('/proc/stat', 'r').readline())
            time.sleep(9)
            stats.append(open('/proc/stat', 'r').readline())
            time.sleep(49)
            stats.append(open('/proc/stat', 'r').readline())
            for session in self.serials:
                session.sendline('rm -f /tmp/cgroup-cpu-lock')

            # Verification
            print stats
            stats[0] = [int(_) for _ in stats[0].split()[1:]]
            stats[0] = [sum(stats[0][0:8]), sum(stats[0][8:])]
            for i in range(1, len(stats)):
                stats[i] = [int(_) for _ in stats[i].split()[1:]]
                try:
                    stats[i] = (float(sum(stats[i][8:]) - stats[0][1]) /
                                        (sum(stats[i][0:8]) - stats[0][0]))
                except ZeroDivisionError:
                    logging.error("ZeroDivisionError in stats calculation")
                    stats[i] = False
            print stats
            for i in range(1, len(stats)):
                if stats[i] < 0.95:
                    raise error.TestFail("Guest time is not >95%% %s" % stats)

            logging.info("Guest times are over 95%%: %s", stats)
            return "Guest times are over 95%%: %s" % stats

    class TestCpusetCpus:
        """
        Tests the cpuset.cpus cgroup feature. It stresses all VM's CPUs
        and changes the CPU affinity. Verifies correct behaviour.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]      # Virt machines
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('cpuset', '')   # cgroup handler
            self.sessions = []


        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            self.sessions[-1].sendline('rm -f /tmp/cgroup-cpu-lock')
            for i in range(len(self.sessions)):
                try:
                    self.sessions[i].close()
                except Exception, failure_detail:
                    err += ("\nCan't close ssh connection %s" % i)

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s" %
                                      err)


        def init(self):
            """
            Prepares cgroup, moves VM into it and execute stressers.
            """
            self.cgroup.initialize(self.modules)
            # We need only machine with more than 1 CPU
            all_cpus = self.cgroup.get_property("cpuset.cpus")[0]
            all_mems = self.cgroup.get_property("cpuset.mems")[0]
            vm_cpus = int(params.get('smp', 1)) # cpus per VM
            if all_cpus == "0" or vm_cpus < 2:
                raise error.TestFail("This test needs at least 2 CPUs on "
                                     "host and the first guest")
            try:
                no_cpus = int(all_cpus.split('-')[1]) + 1
            except Exception:
                raise error.TestFail("Failed to get #CPU from root cgroup.")
            self.cgroup.mk_cgroup()
            self.cgroup.set_property('cpuset.cpus', all_cpus, 0)
            self.cgroup.set_property('cpuset.mems', all_mems, 0)
            assign_vm_into_cgroup(self.vm, self.cgroup, 0)

            cmd = "renice -n 10 $$; " # new ssh login should pass
            cmd += "while [ -e /tmp/cgroup-cpu-lock ]; do :; done"
            for i in range(vm_cpus):
                self.sessions.append(self.vm.wait_for_login(timeout=30))
                self.sessions[i].cmd("touch /tmp/cgroup-cpu-lock")
                self.sessions[i].sendline(cmd)
            self.sessions.append(self.vm.wait_for_login(timeout=30))   # cleanup


        def run(self):
            """
            Actual test; stress VM and verifies the impact on host.
            """
            def _test_it(tst_time):
                """ Helper; gets stat differences during test_time period. """
                _load = get_load_per_cpu()
                time.sleep(tst_time)
                return (get_load_per_cpu(_load)[1:])

            tst_time = 1    # 1s
            vm_cpus = int(params.get('smp', 1)) # cpus per VM
            all_cpus = self.cgroup.get_property("cpuset.cpus")[0]
            no_cpus = int(all_cpus.split('-')[1]) + 1
            stats = []

            # Comments are for vm_cpus=2, no_cpus=4, _SC_CLK_TCK=100
            # All CPUs are used, utilisation should be maximal
            # CPUs: oooo, Stat: 200
            cpus = False
            stats.append((_test_it(tst_time), cpus))

            if no_cpus > vm_cpus:
                # CPUs: OO__, Stat: 200
                cpus = '0-%d' % (vm_cpus-1)
                self.cgroup.set_property('cpuset.cpus', cpus, 0)
                stats.append((_test_it(tst_time), cpus))

            if no_cpus > vm_cpus:
                # CPUs: __OO, Stat: 200
                cpus = "%d-%d" % (no_cpus-vm_cpus-1, no_cpus-1)
                self.cgroup.set_property('cpuset.cpus', cpus, 0)
                stats.append((_test_it(tst_time), cpus))

            # CPUs: O___, Stat: 100
            cpus = "0"
            self.cgroup.set_property('cpuset.cpus', cpus, 0)
            stats.append((_test_it(tst_time), cpus))

            # CPUs: _OO_, Stat: 200
            if no_cpus == 2:
                cpus = "1"
            else:
                cpus = "1-%d" % min(no_cpus, vm_cpus-1)
            self.cgroup.set_property('cpuset.cpus', cpus, 0)
            stats.append((_test_it(tst_time), cpus))

            # CPUs: O_O_, Stat: 200
            cpus = "0"
            for i in range(1, min(vm_cpus, (no_cpus/2))):
                cpus += ",%d" % (i*2)
            self.cgroup.set_property('cpuset.cpus', cpus, 0)
            stats.append((_test_it(tst_time), cpus))

            # CPUs: oooo, Stat: 200
            cpus = False
            self.cgroup.set_property('cpuset.cpus', all_cpus, 0)
            stats.append((_test_it(tst_time), cpus))

            err = ""

            max_cpu_stat = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
            max_stat = max_cpu_stat * vm_cpus
            for i in range(len(stats)):
                if stats[i][1] is False:
                    dist = distance(sum(stats[i][0]), max_stat)
                    if dist > 0.25:
                        err += "%d, " % i
                        logging.error("%s part; incorrect utilisation, dist=%s,"
                                      "%s", i, dist, stats[i])
                    continue    # No restrictions, don't check per_cpu_load

                if stats[i][1].count('-') == 1:
                    # cpus defined by range
                    cpus = []
                    _ = stats[i][1].split('-')
                    for cpu in range(_[0], _[1] + 1):
                        cpus.append(cpu)
                else:
                    # cpus defined by ',' separated list
                    cpus = [int(_) for _ in stats[i][1].split(',')]

                for cpu in range(no_cpus):
                    dist = distance(stats[i][0][cpu], max_cpu_stat)
                    if cpu in cpus:
                        if dist > 0.2:
                            err += "%d, " % cpu
                            logging.error("%s part; per_cpu_load failed; dist="
                                          "%s, cpu=%s, stat=%s", i, dist, cpu,
                                          stats[i])
                    else:
                        if dist < 0.75: # this CPU serves other processes
                            err += "%d, " % i
                            logging.error("%s part; per_cpu_load failed; 1-dist"
                                          "=%s, cpu=%s, stat=%s", i, 1-dist,
                                          cpu, stats[i])

            if err:
                err = "[%s] parts worked incorrectly, check the log" % err[:-2]
                logging.error(err)
                raise error.TestFail(err)

            logging.info("Test passed successfully")
            return ("All clear")


    class TestCpusetCpusSwitching:
        """
        Tests the cpuset.cpus cgroup feature. It stresses all VM's CPUs
        while switching between cgroups with different setting.
        """
        def __init__(self, vms, modules):
            """
            Initialization
            @param vms: list of vms
            @param modules: initialized cgroup module class
            """
            self.vm = vms[0]      # Virt machines
            self.modules = modules          # cgroup module handler
            self.cgroup = Cgroup('cpuset', '')   # cgroup handler
            self.sessions = []


        def cleanup(self):
            """ Cleanup """
            err = ""
            try:
                del(self.cgroup)
            except Exception, failure_detail:
                err += "\nCan't remove Cgroup: %s" % failure_detail

            self.sessions[-1].sendline('rm -f /tmp/cgroup-cpu-lock')
            for i in range(len(self.sessions)):
                try:
                    self.sessions[i].close()
                except Exception, failure_detail:
                    err += ("\nCan't close ssh connection %s" % i)

            if err:
                logging.error("Some cleanup operations failed: %s", err)
                raise error.TestError("Some cleanup operations failed: %s" %
                                      err)


        def init(self):
            """
            Prepares cgroup, moves VM into it and execute stressers.
            """
            self.cgroup.initialize(self.modules)
            vm_cpus = int(params.get('smp', 1))
            all_cpus = self.cgroup.get_property("cpuset.cpus")[0]
            if all_cpus == "0":
                raise error.TestFail("This test needs at least 2 CPUs on "
                                     "host, cpuset=%s" % all_cpus)
            try:
                last_cpu = int(all_cpus.split('-')[1])
            except Exception:
                raise error.TestFail("Failed to get #CPU from root cgroup.")

            if last_cpu == 1:
                last_cpu = "1"
            else:
                last_cpu = "1-%s" % last_cpu

            # Comments are for vm_cpus=2, no_cpus=4, _SC_CLK_TCK=100
            self.cgroup.mk_cgroup() # oooo
            self.cgroup.set_property('cpuset.cpus', all_cpus, 0)
            self.cgroup.set_property('cpuset.mems', 0, 0)
            self.cgroup.mk_cgroup() # O___
            self.cgroup.set_property('cpuset.cpus', 0, 1)
            self.cgroup.set_property('cpuset.mems', 0, 1)
            self.cgroup.mk_cgroup() #_OO_
            self.cgroup.set_property('cpuset.cpus', last_cpu, 2)
            self.cgroup.set_property('cpuset.mems', 0, 2)

            assign_vm_into_cgroup(self.vm, self.cgroup, 0)
            cmd = "renice -n 10 $$; " # new ssh login should pass
            cmd += "while [ -e /tmp/cgroup-cpu-lock ]; do :; done"
            for i in range(vm_cpus):
                self.sessions.append(self.vm.wait_for_login(timeout=30))
                self.sessions[i].cmd("touch /tmp/cgroup-cpu-lock")
                self.sessions[i].sendline(cmd)


        def run(self):
            """
            Actual test; stress VM while simultanously changing the cgroups.
            """
            logging.info("Some harmless IOError messages of non-existing "
                         "processes might occur.")
            i = 0
            t_stop = time.time() + 60 # run for 60 seconds
            while time.time() < t_stop:
                for pid in utils.get_children_pids(self.vm.get_shell_pid()):
                    try:
                        self.cgroup.set_cgroup(int(pid), i % 3)
                    except Exception, inst: # Process might already not exist
                        if os.path.exists("/proc/%s/" % pid):
                            raise error.TestFail("Failed to switch cgroup;"
                                                 " it=%s; %s" % (i, inst))
                i += 1

            self.vm.verify_alive()

            logging.info("Cgroups %s-times successfully switched", i)
            return ("Cgroups %s-times successfully switched" % i)



    # Setup
    # TODO: Add all new tests here
    tests = {"blkio_bandwidth_weigth_read"  : TestBlkioBandwidthWeigthRead,
             "blkio_bandwidth_weigth_write" : TestBlkioBandwidthWeigthWrite,
             "blkio_throttle_read"          : TestBlkioThrottleRead,
             "blkio_throttle_write"         : TestBlkioThrottleWrite,
             "blkio_throttle_multiple_read" : TestBlkioThrottleMultipleRead,
             "blkio_throttle_multiple_write" : TestBlkioThrottleMultipleWrite,
             "devices_access"               : TestDevicesAccess,
             "freezer"                      : TestFreezer,
             "memory_move"                  : TestMemoryMove,
             "memory_limit"                 : TestMemoryLimit,
             "cpu_share_10"                 : TestCpuShare10,
             "cpu_share_50"                 : TestCpuShare50,
             "cpu_cfs_util"                 : TestCpuCFSUtil,
             "cpuset_cpus"                  : TestCpusetCpus,
             "cpuset_cpus_switching"        : TestCpusetCpusSwitching,
            }
    modules = CgroupModules()
    if (modules.init(['blkio', 'cpu', 'cpuset', 'devices', 'freezer',
                      'memory']) <= 0):
        raise error.TestFail('Can\'t mount any cgroup modules')
    # Add all vms
    vms = []
    for vm in params.get("vms", "main_vm").split():
        vm = env.get_vm(vm)
        vm.verify_alive()
        timeout = int(params.get("login_timeout", 360))
        _ = vm.wait_for_login(timeout=timeout)
        _.close()
        del(_)
        vms.append(vm)


    # Execute tests
    results = ""
    # cgroup_tests = "re1[:loops] re2[:loops] ... ... ..."
    for rexpr in params.get("cgroup_tests").split():
        try:
            loops = int(rexpr[rexpr.rfind(':')+1:])
            rexpr = rexpr[:rexpr.rfind(':')]
        except Exception:
            loops = 1
        # number of loops per regular expression
        for _loop in range(loops):
            # cg_test is the subtest name from regular expression
            for cg_test in sorted(
                            [_ for _ in tests.keys() if re.match(rexpr, _)]):
                logging.info("%s: Entering the test", cg_test)
                err = ""
                try:
                    tst = tests[cg_test](vms, modules)
                    tst.init()
                    out = tst.run()
                except error.TestFail, failure_detail:
                    logging.error("%s: Leaving, test FAILED (TestFail): %s",
                                  cg_test, failure_detail)
                    err += "test, "
                    out = failure_detail
                except error.TestError, failure_detail:
                    tb = utils.etraceback(cg_test, sys.exc_info())
                    logging.error("%s: Leaving, test FAILED (TestError): %s",
                                  cg_test, tb)
                    err += "testErr, "
                    out = failure_detail
                except Exception, failure_detail:
                    tb = utils.etraceback(cg_test, sys.exc_info())
                    logging.error("%s: Leaving, test FAILED (Exception): %s",
                                  cg_test, tb)
                    err += "testUnknownErr, "
                    out = failure_detail

                try:
                    tst.cleanup()
                except Exception, failure_detail:
                    logging.warn("%s: cleanup failed: %s\n", cg_test,
                                 failure_detail)
                    err += "cleanup, "

                try:
                    _check_vms(vms)
                except Exception, failure_detail:
                    logging.warn("%s: _check_vms failed: %s\n", cg_test,
                                 failure_detail)
                    err += "VM check, "

                if err.startswith("test"):
                    results += ("\n [F] %s: {%s} FAILED: %s" %
                                 (cg_test, err[:-2], out))
                elif err:
                    results += ("\n [W] %s: Test passed but {%s} FAILED: %s" %
                                 (cg_test, err[:-2], out))
                else:
                    results += ("\n [P] %s: PASSED: %s" % (cg_test, out))

    out = ("SUM: All tests finished (%d PASS / %d WARN / %d FAIL = %d TOTAL)%s"%
           (results.count("\n [P]"), results.count("\n [W]"),
            results.count("\n [F]"), (results.count("\n [P]") +
            results.count("\n [F]") + results.count("\n [W]")), results))
    logging.info(out)
    if results.count("FAILED"):
        raise error.TestFail("Some subtests failed\n%s" % out)
