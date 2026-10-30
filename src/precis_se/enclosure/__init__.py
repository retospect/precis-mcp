"""Moulded/printed enclosure: a drafted shell and a removable locating lid.

The pure millimetre generator emits two CAD scenes in their assembled SI
frame: node dimensions and positions both store bare metres. A rounded
rectangle is a rectangular core, side strips and circular
corners; tilted side strips and conical corners give continuous draft using
existing CAD primitives. Both wall surfaces use the same slope and corner
centres, with the outer surface offset by wall/cos(draft), preserving normal
wall thickness. Inner dimensions are measured at the cavity floor.

The lip follows the opening's draft. Boss disks must fit inside its central
opening with clearance; ribs reach the nearest wall below the lip, so support
cannot obstruct insertion. Pilot holes stay blind above the floor and head
recesses belong exclusively to the lid. Dimensional validation precedes all
CAD nodes. Actual solid intersection is available through lid_overlap_mm3
and pinned by tests, including a shifted-lid collision control.

The SE operation uses prepare/finish halves like flatpack: validation and
owned-block replacement precede deferred CAD writes. Params live in each
part's scene metadata. No new CAD primitive or database schema is needed.
"""

from precis_se.enclosure.generator import Enclosure, enclosure_box, lid_overlap_mm3

__all__ = ["Enclosure", "enclosure_box", "lid_overlap_mm3"]
