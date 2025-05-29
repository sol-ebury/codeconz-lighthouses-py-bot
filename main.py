import argparse
import random
import time
from concurrent import futures

import grpc
from google.protobuf import json_format
from grpc import RpcError

from internal.handler.coms import game_pb2
from internal.handler.coms import game_pb2_grpc as game_grpc

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

        self.already_attacked_lighthouses = set()

        # Board dimensions based on the problem description
        self.board_width = 15
        self.board_height = 15

        # Variables for snake-like pattern movement
        self.starting_corner = None
        self.current_direction = None
        self.moving_horizontally = True
        self.moving_forward = True
        self.visited_cells = set()
        self.return_journey = False
        self.board_covered = False

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y

        lighthouses = dict()
        for lh in turn.Lighthouses:
            lighthouses[(lh.Position.X, lh.Position.Y)] = lh


        # Si estamos en un faro...
        if (cx, cy) in lighthouses:

            # 60% de posibilidades de atacar el faro
            if turn.Energy > lighthouses[(cx, cy)].Energy and (cx, cy) not in self.already_attacked_lighthouses:
                energy = lighthouses[(cx, cy)].Energy + 1
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Energy=energy,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)

                self.countT += 1
                self.already_attacked_lighthouses.add((cx, cy))
                return action

        # Add current position to visited cells
        self.visited_cells.add((cx, cy))

        # Determine starting corner on first turn
        if self.starting_corner is None:
            # Determine which corner we're starting from
            if cx == 0 and cy == 0:  # Upper-left corner
                self.starting_corner = "upper-left"
                self.current_direction = (1, 0)  # Move right initially
                self.moving_horizontally = True
                self.moving_forward = True
            elif cx == self.board_width - 1 and cy == 0:  # Upper-right corner
                self.starting_corner = "upper-right"
                self.current_direction = (-1, 0)  # Move left initially
                self.moving_horizontally = True
                self.moving_forward = False
            elif cx == 0 and cy == self.board_height - 1:  # Bottom-left corner
                self.starting_corner = "bottom-left"
                self.current_direction = (1, 0)  # Move right initially
                self.moving_horizontally = True
                self.moving_forward = True
            elif cx == self.board_width - 1 and cy == self.board_height - 1:  # Bottom-right corner
                self.starting_corner = "bottom-right"
                self.current_direction = (-1, 0)  # Move left initially
                self.moving_horizontally = True
                self.moving_forward = False

        # Check if we need to change direction
        if self.moving_horizontally:
            # Check if we've reached the edge of the board
            if (cx == 0 and not self.moving_forward) or (cx == self.board_width - 1 and self.moving_forward):
                # Move one step vertically
                if self.starting_corner in ["upper-left", "upper-right"]:
                    # Moving down
                    self.current_direction = (0, 1)
                else:
                    # Moving up
                    self.current_direction = (0, -1)

                self.moving_horizontally = False

        else:  # Moving vertically
            # Check if we've moved one step vertically
            if (self.starting_corner in ["upper-left", "upper-right"] and self.current_direction == (0, 1)) or \
               (self.starting_corner in ["bottom-left", "bottom-right"] and self.current_direction == (0, -1)):
                # Switch to horizontal movement in the opposite direction
                self.moving_forward = not self.moving_forward
                if self.moving_forward:
                    self.current_direction = (1, 0)  # Move right
                else:
                    self.current_direction = (-1, 0)  # Move left

                self.moving_horizontally = True

        # Check if we've covered the entire board
        if len(self.visited_cells) >= self.board_width * self.board_height:
            if not self.board_covered:
                self.board_covered = True
                self.return_journey = True
                # Reverse direction for return journey
                self.current_direction = (-self.current_direction[0], -self.current_direction[1])
            elif self.return_journey:
                # Continue zig-zag movement in reverse
                self.return_journey = False
                self.current_direction = (-self.current_direction[0], -self.current_direction[1])

        # Calculate next position
        move = self.current_direction
        next_x = turn.Position.X + move[0]
        next_y = turn.Position.Y + move[1]

        # Ensure we don't move outside the board
        if next_x < 0:
            next_x = 0
        elif next_x >= self.board_width:
            next_x = self.board_width - 1

        if next_y < 0:
            next_y = 0
        elif next_y >= self.board_height:
            next_y = self.board_height - 1

        action = game_pb2.NewAction(
            Action=game_pb2.MOVE,
            Destination=game_pb2.Position(
                X=next_x, Y=next_y
            ),
        )

        bgt = BotGameTurn(turn, action)
        self.turn_states.append(bgt)

        self.countT += 1
        return action


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