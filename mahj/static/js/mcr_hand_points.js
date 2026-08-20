/* The four per-seat deltas for one played MCR hand.
   Every hand moves money around an 8-point base: a discard win pays the winner
   8 from each seat plus the hand's value from the discarder; a self-draw pays
   8-plus-value from all three. One definition, shared by the editable score
   sheet and the read-only detail modal — each keeps only its own rendering. */

/* `points`: the hand's value. `winner`/`discarder`: seat winds (1-4), where
   the discarder may be NaN or 0 for "none" (a self-draw). Returns four deltas
   positioned by seat, or null when there is no playable win to distribute:
   no winning seat, or a discarder outside 0-4 — the same cells the sheet
   already flags red. */
window.mcrHandPointDeltas = function (points, winner, discarder) {
  if (isNaN(winner) || winner < 1 || winner > 4) return null;
  if (!isNaN(discarder) && (discarder < 0 || discarder > 4)) return null;
  var selfDraw = isNaN(discarder) || discarder === 0 || discarder === winner;
  var deltas = [];
  for (var seat = 1; seat <= 4; seat++) {
    if (selfDraw)               deltas.push(seat === winner ? 3 * (8 + points) : -(8 + points));
    else if (seat === winner)   deltas.push(3 * 8 + points);
    else if (seat === discarder) deltas.push(-(8 + points));
    else                        deltas.push(-8);
  }
  return deltas;
};
