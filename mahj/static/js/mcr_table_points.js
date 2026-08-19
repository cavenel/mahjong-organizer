/* MCR table points for one table, derived from its four minipoint totals.
   Seats rank on minipoints, highest first, and the four places are worth
   4 / 2 / 1 / 0 table points. Seats tied on minipoints share the average of
   the places they cover, so a two-way tie for first gives both (4+2)/2 = 3.

   The score-entry grids compute this client-side to pre-fill the table-point
   cells as the scorer types; the value the scorer saves is what counts, so this
   must agree with what a head scorer would work out by hand. One definition,
   shared by every grid that shows it. */

/* `minipoints`: array of four numbers. Returns four table points positioned
   like the input. */
window.tablePointsFromMinipoints = function (minipoints) {
  // Seat indices, best minipoints first.
  var byRank = minipoints.map(function (_, i) { return i; }).sort(function (a, b) {
    return minipoints[a] < minipoints[b] ? 1 : minipoints[a] > minipoints[b] ? -1 : 0;
  });
  var placePoints = [4, 2, 1, 0];
  // Group the seats tied on minipoints together with the places they cover.
  var groups = [], groupPoints = [], previousMp = Infinity;
  for (var i = 0; i < byRank.length; i++) {
    if (minipoints[byRank[i]] !== previousMp) {
      groups.push([]);
      groupPoints.push([]);
      previousMp = minipoints[byRank[i]];
    }
    groups[groups.length - 1].push(byRank[i]);
    groupPoints[groupPoints.length - 1].push(placePoints[i]);
  }
  var tp = [null, null, null, null];
  for (var g = 0; g < groups.length; g++) {
    var shared = groupPoints[g].reduce(function (a, b) { return a + b; }, 0) / groupPoints[g].length;
    groups[g].forEach(function (seat) { tp[seat] = shared; });
  }
  return tp;
};
