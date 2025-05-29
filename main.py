import argparse
import enum
import random
import time
from concurrent import futures

import grpc
from google.protobuf import json_format
from grpc import RpcError

from internal.handler.coms import game_pb2
from internal.handler.coms import game_pb2_grpc as game_grpc

# Define all possible movement vectors as a dictionary for better readability
NAVIGATION_VECTORS = {
    'NORTHWEST': (-1, -1),
    'WEST': (-1, 0),
    'SOUTHWEST': (-1, 1),
    'SOUTH': (0, -1),
    'NORTH': (0, 1),
    'NORTHEAST': (1, 1),
    'EAST': (1, 0),
    'SOUTHEAST': (1, -1)
}

# Convert dictionary to tuple for compatibility with existing code
MOVES = tuple(NAVIGATION_VECTORS.values())


class Movements(enum.Enum):
    """Enumeration of all possible movement directions with descriptive names"""
    NORTHWEST = (-1, -1)  # Diagonal movement: up and left
    WEST = (-1, 0)        # Horizontal movement: left
    SOUTHWEST = (-1, 1)   # Diagonal movement: down and left
    NORTH = (0, 1)        # Vertical movement: up
    NORTHEAST = (1, 1)    # Diagonal movement: up and right
    EAST = (1, 0)         # Horizontal movement: right
    SOUTHEAST = (1, -1)   # Diagonal movement: down and right
    SOUTH = (0, -1)       # Vertical movement: down

timeout_to_response = 1  # 1 second


class BotGameTurn:
    def __init__(self, turn, action):
        self.turn = turn
        self.action = action


class BotGame:
    def __init__(self, player_num=None):
        self.player_num = player_num
        self.initial_state = None
        self.turn_states = []
        self.countT = 1

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y

        lighthouses = dict()
        for lh in turn.Lighthouses:
            lighthouses[(lh.Position.X, lh.Position.Y)] = lh

        # Si estamos en un faro...
        if (cx, cy) in lighthouses:
            # Conectar con faro remoto válido si podemos
            if lighthouses[(cx, cy)].Owner == self.player_num:
                possible_connections = []
                for dest in lighthouses:
                    # No conectar con sigo mismo
                    # No conectar si no tenemos la clave
                    # No conectar si ya existe la conexión
                    # No conectar si no controlamos el destino
                    # Nota: no comprobamos si la conexión se cruza.
                    if (
                        dest != (cx, cy)
                        and lighthouses[dest].HaveKey
                        and [cx, cy] not in lighthouses[dest].Connections
                        and lighthouses[dest].Owner == self.player_num
                    ):
                        possible_connections.append(dest)

                if possible_connections:
                    possible_connection = random.choice(possible_connections)
                    action = game_pb2.NewAction(
                        Action=game_pb2.CONNECT,
                        Destination=game_pb2.Position(
                            X=possible_connection[0], Y=possible_connection[1]
                        ),
                    )
                    bgt = BotGameTurn(turn, action)
                    self.turn_states.append(bgt)

                    self.countT += 1
                    return action

            # 60% de posibilidades de atacar el faro
            if random.randrange(100) < 60:
                energy = random.randrange(turn.Energy + 1)
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Energy=energy,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)

                self.countT += 1
                return action

        # Mover aleatoriamente

        # Buscar el faro apropiado basado en el ratio
        # Movernos en la direcciñon adecuada, dandole nuestra posicion y la del faro que buscamos

        # Determine strategy based on how many lighthouses we control
        controlled_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner == self.player_num]
        control_count = len(controlled_lighthouses)

        # If we control many lighthouses, focus on defending what we have
        if control_count > 15:
            # Simply target the first lighthouse we own
            target_lighthouse = controlled_lighthouses[0]
            chosen_lighthouse = target_lighthouse
        else:
            # Offensive strategy: evaluate and target the most valuable lighthouse
            lighthouse_evaluations = {}

            # Calculate value score for each lighthouse
            for beacon in turn.Lighthouses:
                # Calculate efficiency score based on distance and energy
                value_score = self.compute_ratio(turn.Position, beacon)
                # Store score with lighthouse coordinates and owner as key
                position_key = (beacon.Position.X, beacon.Position.Y, beacon.Owner)
                lighthouse_evaluations[position_key] = value_score

            # Select the best lighthouse to target (not already owned)
            chosen_lighthouse = self.get_chosen_non_conquered_lighthouse(lighthouse_evaluations)
        next_movement = self.get_next_movement(turn.Position, chosen_lighthouse)
        move = next_movement
        action = game_pb2.NewAction(
            Action=game_pb2.MOVE,
            Destination=game_pb2.Position(
                X=turn.Position.X + move[0], Y=turn.Position.Y + move[1]
            ),
        )

        bgt = BotGameTurn(turn, action)
        self.turn_states.append(bgt)

        self.countT += 1
        return action

    def compute_ratio(self, current_pos, target_lighthouse):
        """
        Calculate a value score for a lighthouse based on energy required and distance
        Lower energy and shorter distance results in a higher score (more desirable)
        """
        # Extract the energy level of the lighthouse
        power_required = target_lighthouse.Energy

        # Calculate Manhattan distance (sum of horizontal and vertical distances)
        manhattan_dist = (
            abs(current_pos.X - target_lighthouse.Position.X) +
            abs(current_pos.Y - target_lighthouse.Position.Y)
        )

        # Calculate the efficiency score - inverse relationship with energy and distance
        # Adding 1 to avoid division by zero issues
        efficiency_score = 1.0 / ((power_required + 1) * (manhattan_dist + 1))

        return efficiency_score

    def get_chosen_non_conquered_lighthouse(self, lighthouse_scores):
        """
        Select the optimal lighthouse to target based on calculated scores
        Only considers lighthouses not already owned by this player
        """
        # Filter out lighthouses we already own
        available_targets = {
            coords: score
            for coords, score in lighthouse_scores.items()
            if coords[2] != self.player_num  # Check owner ID in the tuple
        }

        # Return the lighthouse with the highest score, or None if no valid targets
        if available_targets:
            return max(available_targets, key=available_targets.get)
        return None

    def get_next_movement(self, current_pos, destination):
        """
        Determine the optimal direction to move toward the target lighthouse
        Prioritizes vertical movement first, then horizontal if needed
        """
        # Calculate vertical and horizontal displacement
        vertical_offset = destination[1] - current_pos.Y
        horizontal_offset = destination[0] - current_pos.X

        # Determine movement direction based on offsets
        if vertical_offset > 0:  # Need to move up
            return Movements.NORTH.value
        elif vertical_offset < 0:  # Need to move down
            return Movements.SOUTH.value
        elif horizontal_offset > 0:  # Need to move right
            return Movements.EAST.value
        else:  # Need to move left (or no movement needed)
            return Movements.WEST.value

class BotComs:
    def __init__(self, bot_name, my_address, game_server_address, verbose=False):
        self.bot_id = None
        self.bot_name = bot_name
        self.my_address = my_address
        self.game_server_address = game_server_address
        self.verbose = verbose

    def wait_to_join_game(self):
        channel = grpc.insecure_channel(self.game_server_address)
        client = game_grpc.GameServiceStub(channel)

        player = game_pb2.NewPlayer(name=self.bot_name, serverAddress=self.my_address)

        while True:
            try:
                player_id = client.Join(player, timeout=timeout_to_response)
                self.bot_id = player_id.PlayerID
                print(f"Joined game with ID {player_id.PlayerID}")
                if self.verbose:
                    print(json_format.MessageToJson(player_id))
                break
            except RpcError as e:
                print(f"Could not join game: {e.details()}")
                time.sleep(1)

    def start_listening(self):
        print("Starting to listen on", self.my_address)

        # configure gRPC server
        grpc_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            interceptors=(ServerInterceptor(),),
        )

        # registry of the service
        cs = ClientServer(bot_id=self.bot_id, verbose=self.verbose)
        game_grpc.add_GameServiceServicer_to_server(cs, grpc_server)

        # server start
        grpc_server.add_insecure_port(self.my_address)
        grpc_server.start()

        try:
            grpc_server.wait_for_termination()  # wait until server finish
        except KeyboardInterrupt:
            grpc_server.stop(0)


class ServerInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        start_time = time.time_ns()
        method_name = handler_call_details.method

        # Invoke the actual RPC
        response = continuation(handler_call_details)

        # Log after the call
        duration = time.time_ns() - start_time
        print(f"Unary call: {method_name}, Duration: {duration:.2f} nanoseconds")
        return response


class ClientServer(game_grpc.GameServiceServicer):
    def __init__(self, bot_id, verbose=False):
        self.bg = BotGame(bot_id)
        self.verbose = verbose

    def Join(self, request, context):
        return None

    def InitialState(self, request, context):
        print("Receiving InitialState")
        if self.verbose:
            print(json_format.MessageToJson(request))
        self.bg.initial_state = request
        return game_pb2.PlayerReady(Ready=True)

    def Turn(self, request, context):
        print(f"Processing turn: {self.bg.countT}")
        if self.verbose:
            print(json_format.MessageToJson(request))
        action = self.bg.new_turn_action(request)
        return action


def ensure_params():
    parser = argparse.ArgumentParser(description="Bot configuration")
    parser.add_argument("--bn", type=str, default="random-bot", help="Bot name")
    parser.add_argument("--la", type=str, required=True, help="Listen address")
    parser.add_argument("--gs", type=str, required=True, help="Game server address")

    args = parser.parse_args()

    if not args.bn:
        raise ValueError("Bot name is required")
    if not args.la:
        raise ValueError("Listen address is required")
    if not args.gs:
        raise ValueError("Game server address is required")

    return args.bn, args.la, args.gs


def main():
    verbose = False
    bot_name, listen_address, game_server_address = ensure_params()

    bot = BotComs(
        bot_name=bot_name,
        my_address=listen_address,
        game_server_address=game_server_address,
        verbose=verbose,
    )
    bot.wait_to_join_game()
    bot.start_listening()


if __name__ == "__main__":
    main()