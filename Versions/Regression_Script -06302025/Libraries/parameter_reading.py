
import argparse

def argparse_():
    parser = argparse.ArgumentParser(
        prog="Data Validation Process",
        description="This process will validate the Data at multiple stages",
        epilog="Nirvana health",
        formatter_class=argparse.RawTextHelpFormatter
    )

    # Add Arguments
    # Module Name
    parser.add_argument(
        "-m", "--module",
        type=str,
        help="""Module name  - (Required)  
                M3P""",
        required=True,

    )
    # Stage Type
    parser.add_argument(
        "-s", "--stage",
        type=str,
        help= """Validation Stage Type
            Initial Testing """,
        default=None,

    )

    # Cycle Type
    parser.add_argument(
        "-c", "--cycle",
        type=str,
        help= """Validation Cycle Types  - (Optional)  
                M3P Enrollment """,
        default=None
    )

    # Test case Order
    parser.add_argument(
        "-t", "--testcasenumber",
        type=str,
        help= f"""To the specific order 
                0 = all
                0-N = run for the specific order 
        """,
        default="0"
    )

    parser.add_argument(
        "-e", "--environment",
        type=str,
        help=f"""Environment where Data_Regression_Set collection located 
                   MCPQA1,EHQA,IMPL
           """,
        default= "MCPQA1"
    )

    return parser.parse_args()

