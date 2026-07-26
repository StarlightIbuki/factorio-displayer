Phase 1: Placement with "Soft" Distance Limits
Previously, the Simulated Annealing (SA) placement algorithm would instantly reject a layout if any connection was > 7 tiles. We must change this to a Soft Constraint.
The New Cost Function: When evaluating a layout, if a connection between two ports (or a port and the rest of its Net) is greater than 7 tiles, it doesn't fail. Instead, we estimate the number of poles needed using simple math: Estimated_Poles = Floor(Distance / 7).
The Penalty: We multiply Estimated_Poles by a high penalty weight. This teaches the algorithm: "It is better to pack combinators closely to avoid poles. But if you MUST use a pole to solve a complex overlap, you are allowed to."
This allows the SA to settle on a highly compact arrangement of combinators, leaving the exact physical placement of the poles for the next phase.
Phase 2: Grid-Based A* "Flying" Router (Pole Insertion)
Once the combinators are placed, we map the layout to a 2D grid. Every tile is marked as either Occupied (by a combinator) or Empty.
Instead of a simple Minimum Spanning Tree, we use a custom A Pathfinding Algorithm* to route the wires and drop poles. Think of the pathfinder as a drone that flies over the board:
Rule 1: The drone can "fly" over occupied combinators (wires don't collide).
Rule 2: The drone can only fly a maximum of 7 tiles from its last anchor point.
Rule 3: To fly further, it must "land" and place a 1x1 Pole.
Rule 4: It can ONLY land on an Empty tile.
The A Cost Function (Minimizing Poles):*
As the A* algorithm searches for a path from Port A to Port B, it accumulates "Cost":
Flying 1 tile distance: Cost +1
Landing on an empty tile to drop a NEW pole: Cost +1000 (Massive penalty to ensure we only drop them when strictly necessary).
Because A* naturally finds the path of least resistance, it will perfectly bridge gaps > 7 by finding the optimal empty 1x1 spaces to drop poles, using the absolute minimum number of them.
Phase 3: The "Pole Sharing" Optimization (Crucial)
Because a single pole can carry both Red and Green wires, we can save space and minimize pole counts by sharing them. If we route Red and Green independently, we might accidentally put a Red Pole and a Green pole right next to each other, wasting a 1x1 tile.
How to solve this:
Route the longest Nets first, regardless of color. Let's say we route a massive Red Net. The A* drone drops 3 poles to bridge a long gap.
Update the Grid: Those 3 specific 1x1 tiles are no longer "Empty". They are now marked as "Existing Poles".
Route the next Nets: When routing a Green Net later, we update the A* drone's cost function:
Dropping a NEW pole on an empty tile: Cost +1000
Landing on an "Existing Pole": Cost +0 (It's free!)
By making existing poles "free" to land on, the A* algorithm will naturally bend the path of Green wires to "hitch a ride" on the poles created by the Red wires, perfectly mimicking how human Factorio players build neat, shared wiring buses!
Phase 4: Handling "Routing Blockages" (Rip-up and Reroute)
What happens if the algorithm packs the combinators so tightly that a wire needs to go 15 tiles, but there are absolutely zero empty 1x1 tiles available to drop a pole? The A* drone crashes. This is called a Routing Blockage.
We handle this using a classic EDA technique called Rip-up and Reroute:
If A* fails to find a valid landing spot for a pole, the Routing Phase pauses.
We identify the combinators that are blocking the ideal flight path.
We artificially "inflate" the physical size of those blocking combinators in the Placement Engine's memory (e.g., we tell the engine a 1x2 combinator is actually 2x3).
We kick it back to Phase 1 (Placement). The Simulated Annealing algorithm runs again, but because of the inflated sizes, it is forced to push the combinators slightly apart, naturally creating 1x1 empty gaps.
Try Routing (Phase 2) again.
Summary of the System So Far
Logical Netlist: Defines 1x1/1x2 rigid entities, their bound Input/Output ports, and the overlapping Red/Green logical Nets.
Simulated Annealing Placement: Places and rotates combinators to minimize overall area and theoretical wire distance, with a soft penalty for distances > 7.
A Flying Router:* Routes wires by flying over entities, forcefully dropping 1x1 Poles on empty tiles every 7 tiles.
Pole Hitchhiking: Subsequent nets seek out previously placed poles to share them for free, minimizing the total pole count.
Rip-up & Reroute: Automatically loosens the layout to create 1x1 gaps if the placement was too dense to fit required poles.