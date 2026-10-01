# NFS export stall wedges only the Linux clients (lockd)

**Symptom.** `ls /mnt/cluster` hangs on every **Linux** client (spark, castor,
pollux) with worker/ansible processes stuck in **D-state**. The macOS clients
(melchior, balthazar) stay healthy; on caspar itself the export dir may
transiently hang too. `showmount -e`, ping and tcp/2049 all look fine — it
looks like a network fault but isn't. (Seen 2026-08-29Z.)

**Diagnostic shortcut.** If `mount -o nolock` works while a normal mount hangs,
it is **server-side lockd/statd** → restart nfsd on caspar, not a network fix.

## Fix, in order

1. `ssh caspar 'sudo nfsd restart'` (lockd can't be kicked separately — SIP).
2. On each Linux client:

       sudo pkill -9 mount.nfs; sudo umount -f -l /mnt/cluster; sudo mount /mnt/cluster

   After the server restart, standard (locking) mounts work again; before it,
   only `-o nolock` mounts succeeded.
3. Old D-state processes referencing the lazily-unmounted fs linger
   unkillable but harmless; they clear on the next reboot.

## Separate gotcha, same evening

**ansible `pipelining=True` hangs intermittently against castor/pollux** (the
remote become python polls stdin forever for the module payload).
`ANSIBLE_PIPELINING=False` fixes it immediately — try that before debugging
any "hung deploy" on the twins (gripe filed 2026-08-29Z).
See [`cluster-deploy`](./cluster-deploy.md).
